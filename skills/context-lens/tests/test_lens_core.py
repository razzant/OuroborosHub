"""Tests for the Context Lens ledger reader and aggregator.

Dependency-free: standard-library ``unittest`` only, fixtures written into a
temporary directory. Nothing here touches a real Ouroboros install.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lens_core = _load("context_lens_core_under_test", "lens_core.py")


# ---------------------------------------------------------------------------
# Fixture builder — mirrors the exact row shapes usage_accounting writes.
# ---------------------------------------------------------------------------

class Ledger:
    def __init__(self) -> None:
        self.rows = []

    def _next(self) -> int:
        return len(self.rows) + 1

    def add(self, **row) -> dict:
        row.setdefault("kind", "attempt")
        row.setdefault("ts", "2026-09-07T10:%02d:00+00:00" % (self._next() % 60))
        row["seq"] = self._next()
        self.rows.append(row)
        return row

    def attempt(
        self,
        attempt_id: str,
        *,
        model: str = "vendor/model-a",
        provider: str = "openrouter",
        category: str = "task",
        source: str = "llm",
        task_id: str = "task-1",
        root_task_id: str = "task-1",
        parent_task_id: str = "",
        prompt_tokens=None,
        completion_tokens=None,
        cached_tokens=None,
        final: str = "settled",
        physical_context=None,
        reserved: bool = True,
        dispatched: bool = True,
        ts_reserved: str = "2026-09-07T10:00:00+00:00",
        ts_final: str = "2026-09-07T10:00:05+00:00",
    ) -> None:
        common = {
            "attempt_id": attempt_id, "model": model, "provider": provider,
            "category": category, "source": source, "task_id": task_id,
            "root_task_id": root_task_id, "parent_task_id": parent_task_id,
        }
        if physical_context is not None:
            common["physical_context"] = physical_context
        if reserved:
            self.add(state="reserved", ts=ts_reserved, **common)
        if dispatched:
            self.add(state="dispatched", ts=ts_reserved, **common)
        if final:
            tokens = {}
            if final == "settled":
                tokens = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cached_tokens": cached_tokens,
                }
            self.add(state=final, ts=ts_final, **common, **tokens)

    def write(self, root: str, *, trailing_newline: bool = True, extra: str = "") -> str:
        state_dir = os.path.join(root, "state")
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, "usage_attempts.jsonl")
        payload = "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in self.rows
        )
        if not trailing_newline and payload.endswith("\n"):
            payload = payload[:-1]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload + extra)
        return path


FIT_MAX = {
    "profile": "owner_max", "rendered_mode": "max",
    "measurement_basis": "fresh_route_usage", "route_fp": "a" * 64,
    "round_id": "round-1", "target_total_tokens": 180000,
    "capacity_total_tokens": 200000, "context_target_miss": False,
    "automatic_pass_used": False,
}
FIT_LOW = dict(FIT_MAX, profile="owner_low", rendered_mode="low",
               target_total_tokens=40000, capacity_total_tokens=60000)


class LensTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="context-lens-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def window(self, ledger: Ledger, **kwargs) -> "lens_core.LedgerWindow":
        ledger.write(self.root)
        return lens_core.LedgerWindow(self.root, **kwargs)


# ---------------------------------------------------------------------------


class TestNumericAggregation(LensTestCase):
    def test_settled_attempt_folds_into_one_point(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=120000, completion_tokens=800,
                       cached_tokens=90000, physical_context=FIT_MAX)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()

        self.assertTrue(snapshot["ok"])
        self.assertEqual(len(snapshot["points"]), 1)
        point = snapshot["points"][0]
        self.assertEqual(point["prompt_tokens"], 120000)
        self.assertEqual(point["cached_tokens"], 90000)
        self.assertEqual(point["mode"], "max")
        self.assertEqual(point["capacity_total_tokens"], 200000)
        self.assertEqual(point["states"], ["reserved", "dispatched", "settled"])
        self.assertEqual(point["elapsed_sec"], 5.0)
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)
        self.assertEqual(snapshot["counters"]["measured"], 1)

    def test_cached_tokens_are_never_added_to_prompt_tokens(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=100000, cached_tokens=95000)
        window = self.window(ledger)
        window.refresh()
        point = window.snapshot()["points"][0]
        self.assertEqual(point["prompt_tokens"], 100000)
        self.assertLess(point["cached_tokens"], point["prompt_tokens"])
        summary = lens_core.spread(window.snapshot()["points"])
        self.assertEqual(summary["peak"], 100000)

    def test_spread_statistics(self) -> None:
        ledger = Ledger()
        for index, size in enumerate([10, 20, 30, 40, 100]):
            ledger.attempt("a%d" % index, prompt_tokens=size * 1000)
        window = self.window(ledger)
        window.refresh()
        summary = lens_core.spread(window.snapshot()["points"])
        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["median"], 30000)
        self.assertEqual(summary["peak"], 100000)
        self.assertEqual(summary["low"], 10000)
        self.assertAlmostEqual(summary["p95"], 88000.0, places=3)

    def test_quantile_edges(self) -> None:
        self.assertIsNone(lens_core.quantile([], 0.5))
        self.assertEqual(lens_core.quantile([7], 0.95), 7.0)
        self.assertEqual(lens_core.quantile([0, 10], 0.5), 5.0)


class TestMissingAndEmpty(LensTestCase):
    def test_empty_ledger(self) -> None:
        window = self.window(Ledger())
        window.refresh()
        snapshot = window.snapshot()
        self.assertEqual(snapshot["points"], [])
        self.assertEqual(snapshot["counters"]["physical_attempts"], 0)
        self.assertEqual(snapshot["facets"]["models"], [])

    def test_missing_tokens_are_not_zero(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=None, completion_tokens=None)
        window = self.window(ledger)
        window.refresh()
        point = window.snapshot()["points"][0]
        self.assertIsNone(point["prompt_tokens"])
        self.assertIsNone(point["completion_tokens"])
        counters = window.snapshot()["counters"]
        self.assertEqual(counters["measured"], 0)
        self.assertEqual(counters["settled_without_tokens"], 1)

    def test_tokens_on_a_non_settled_row_never_measure_the_request(self) -> None:
        """A stray token count on a pre-terminal row is not evidence of a size.

        ``usage_ledger._validate_records`` structurally permits token fields on
        a ``reserved``/``dispatched`` row, so a corrupt or future chain can put
        one there and omit it on the settled row. The settled row is the only
        authority in both directions: it assigns all four token fields, ``None``
        included, so the earlier value cannot survive and be reported measured.
        """
        common = {
            "attempt_id": "a1", "model": "vendor/model-a", "provider": "openrouter",
            "category": "task", "source": "llm", "task_id": "task-1",
            "root_task_id": "task-1", "parent_task_id": "",
        }
        ledger = Ledger()
        ledger.add(state="reserved", ts="2026-09-07T10:00:00+00:00",
                   prompt_tokens=999_999, completion_tokens=4242,
                   cached_tokens=777, cache_write_tokens=555, **common)
        ledger.add(state="dispatched", ts="2026-09-07T10:00:01+00:00",
                   prompt_tokens=888_888, **common)
        # The settled row carries no token fields at all.
        ledger.add(state="settled", ts="2026-09-07T10:00:05+00:00", **common)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()

        point = snapshot["points"][0]
        self.assertEqual(point["state"], "settled")
        self.assertIsNone(point["prompt_tokens"])
        self.assertIsNone(point["completion_tokens"])
        self.assertIsNone(point["cached_tokens"])
        self.assertIsNone(point["cache_write_tokens"])
        counters = snapshot["counters"]
        self.assertEqual(counters["measured"], 0)
        self.assertEqual(counters["settled_without_tokens"], 1)

    def test_an_explicit_null_on_the_settled_row_clears_an_earlier_value(self) -> None:
        common = {
            "attempt_id": "a1", "model": "vendor/model-a", "provider": "openrouter",
            "category": "task", "source": "llm", "task_id": "task-1",
            "root_task_id": "task-1", "parent_task_id": "",
        }
        ledger = Ledger()
        ledger.add(state="reserved", ts="2026-09-07T10:00:00+00:00",
                   prompt_tokens=120_000, **common)
        ledger.add(state="settled", ts="2026-09-07T10:00:05+00:00",
                   prompt_tokens=None, completion_tokens=None,
                   cached_tokens=None, cache_write_tokens=None, **common)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        self.assertIsNone(snapshot["points"][0]["prompt_tokens"])
        self.assertEqual(snapshot["counters"]["measured"], 0)
        self.assertEqual(snapshot["counters"]["settled_without_tokens"], 1)

    def test_a_released_row_after_a_settled_one_keeps_the_settled_tokens(self) -> None:
        """Only a settled row assigns tokens; a later non-settled row is inert."""
        common = {
            "attempt_id": "a1", "model": "vendor/model-a", "provider": "openrouter",
            "category": "task", "source": "llm", "task_id": "task-1",
            "root_task_id": "task-1", "parent_task_id": "",
        }
        ledger = Ledger()
        ledger.add(state="settled", ts="2026-09-07T10:00:05+00:00",
                   prompt_tokens=120_000, **common)
        ledger.add(state="released", ts="2026-09-07T10:00:06+00:00",
                   prompt_tokens=1, **common)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        point = snapshot["points"][0]
        self.assertEqual(point["state"], "released")
        # The later released row's stray count is inert: the settled row's
        # value stands, and it is not counted as a measured request either.
        self.assertEqual(point["prompt_tokens"], 120_000)
        counters = snapshot["counters"]
        self.assertEqual(counters["by_state"]["released"], 1)
        self.assertEqual(counters["measured"], 0)

    def test_in_flight_and_terminal_states(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", final="", dispatched=False)            # reserved
        ledger.attempt("a2", final="", dispatched=True)             # dispatched
        ledger.attempt("a3", final="unresolved")
        ledger.attempt("a4", final="released", dispatched=False)
        ledger.attempt("a5", prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        counters = window.snapshot()["counters"]
        self.assertEqual(counters["by_state"],
                         {"reserved": 1, "dispatched": 1, "settled": 1,
                          "unresolved": 1, "released": 1})
        self.assertEqual(counters["in_flight"], 2)
        self.assertEqual(counters["physical_attempts"], 5)
        self.assertEqual(counters["measured"], 1)

    def test_missing_ledger_file(self) -> None:
        window = lens_core.LedgerWindow(self.root)
        with self.assertRaises(lens_core.LensUnavailable) as caught:
            window.refresh()
        self.assertEqual(caught.exception.code, "no_ledger")

    def test_missing_data_dir(self) -> None:
        with self.assertRaises(lens_core.LensUnavailable) as caught:
            lens_core.LedgerWindow("")
        self.assertEqual(caught.exception.code, "no_data_dir")


class TestMalformedInput(LensTestCase):
    def test_broken_json_line_is_counted_and_skipped(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        path = ledger.write(self.root)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
            handle.write(json.dumps({"seq": 99, "attempt_id": "a2", "state": "settled",
                                     "prompt_tokens": 2000}) + "\n")
        window = lens_core.LedgerWindow(self.root)
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 1)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 2)

    def test_rows_without_identity_are_malformed(self) -> None:
        path = os.path.join(self.root, "state", "usage_attempts.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"seq": 1, "state": "settled"}) + "\n")        # no attempt_id
            handle.write(json.dumps({"attempt_id": "x", "state": "settled"}) + "\n")  # no seq
            handle.write(json.dumps(["not", "an", "object"]) + "\n")
            handle.write(json.dumps({"seq": 4, "attempt_id": "ok", "state": "settled",
                                     "prompt_tokens": 5}) + "\n")
        window = lens_core.LedgerWindow(self.root)
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 3)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 1)

    def test_torn_tail_row_is_left_for_the_next_refresh(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        path = ledger.write(self.root)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"seq":4,"attempt_id":"a2","state":"settle')  # no newline yet
        window = lens_core.LedgerWindow(self.root)
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 0)
        self.assertGreater(result["pending_tail_bytes"], 0)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 1)

        # The writer finishes the row; the next refresh picks it up exactly once.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('d","prompt_tokens":2000,"model":"vendor/model-a"}\n')
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 0)
        self.assertEqual(result["pending_tail_bytes"], 0)
        snapshot = window.snapshot()
        self.assertEqual(snapshot["counters"]["physical_attempts"], 2)
        self.assertEqual(snapshot["counters"]["measured"], 2)

    def test_non_utf8_bytes_are_malformed_not_fatal(self) -> None:
        path = os.path.join(self.root, "state", "usage_attempts.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe not utf8\n")
            handle.write(json.dumps({"seq": 2, "attempt_id": "ok", "state": "settled",
                                     "prompt_tokens": 7}).encode("utf-8") + b"\n")
        window = lens_core.LedgerWindow(self.root)
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 1)
        self.assertEqual(window.snapshot()["counters"]["measured"], 1)


class TestUniquenessAndCache(LensTestCase):
    def test_repeated_refresh_does_not_double_count(self) -> None:
        ledger = Ledger()
        for index in range(5):
            ledger.attempt("a%d" % index, prompt_tokens=1000 * (index + 1))
        window = self.window(ledger)
        for _ in range(4):
            window.refresh()
        counters = window.snapshot()["counters"]
        self.assertEqual(counters["physical_attempts"], 5)
        self.assertEqual(counters["measured"], 5)
        self.assertEqual(len(window.snapshot()["points"]), 5)

    def test_incremental_append_is_read_once(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        path = ledger.write(self.root)
        window = lens_core.LedgerWindow(self.root)
        first = window.refresh()
        self.assertEqual(first["lines_read"], 3)

        follow = Ledger()
        follow.rows = list(ledger.rows)
        follow.attempt("a2", prompt_tokens=2000)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in follow.rows
            ))
        second = window.refresh()
        self.assertEqual(second["lines_read"], 6)   # 3 old + 3 new, never re-read
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 2)

    def test_one_attempt_chain_is_one_record(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)      # three rows, one attempt
        window = self.window(ledger)
        result = window.refresh()
        self.assertEqual(result["lines_read"], 3)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 1)

    def test_record_cache_is_bounded_and_discloses_eviction(self) -> None:
        ledger = Ledger()
        for index in range(12):
            ledger.attempt("a%d" % index, prompt_tokens=1000 + index,
                           reserved=False, dispatched=False)
        window = self.window(ledger, max_records=5)
        result = window.refresh()
        self.assertEqual(result["max_records"], 5)
        self.assertEqual(result["evicted_records"], 7)
        snapshot = window.snapshot()
        self.assertEqual(snapshot["counters"]["physical_attempts"], 5)
        self.assertEqual([point["prompt_tokens"] for point in snapshot["points"]],
                         [1007, 1008, 1009, 1010, 1011])

    def test_point_limit_discloses_omission(self) -> None:
        ledger = Ledger()
        for index in range(10):
            ledger.attempt("a%d" % index, prompt_tokens=1000,
                           reserved=False, dispatched=False)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot(limit=4)
        self.assertEqual(len(snapshot["points"]), 4)
        self.assertEqual(snapshot["points_omitted"], 6)
        self.assertEqual(snapshot["counters"]["physical_attempts"], 10)


class TestBoundsAndRotation(LensTestCase):
    def test_cold_tail_read_discloses_the_omitted_prefix(self) -> None:
        ledger = Ledger()
        for index in range(200):
            ledger.attempt("a%d" % index, prompt_tokens=1000 + index,
                           reserved=False, dispatched=False)
        window = self.window(ledger, max_bytes=4096)
        result = window.refresh()
        self.assertGreater(result["omitted_prefix_bytes"], 0)
        self.assertLess(window.snapshot()["counters"]["physical_attempts"], 200)
        # A truncated leading line is never parsed as a row.
        self.assertEqual(result["malformed_lines"], 0)

    # -- the cold tail that lands inside an unterminated line -----------------
    def _cold_tail_inside_a_line(self):
        """A file whose last 4096 bytes hold no newline at all.

        The cold tail therefore starts inside a line that has no terminator
        anywhere in the chunk — the case that used to advance the offset and
        turn the remainder of that line into a malformed row later on.
        """
        path = os.path.join(self.root, "state", "usage_attempts.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        good = json.dumps({"seq": 1, "attempt_id": "a1", "state": "settled",
                           "prompt_tokens": 1000}) + "\n"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(good * 100)                       # complete rows, all older
            handle.write('{"seq":2,"attempt_id":"a2","note":"' + "x" * 4200)
        window = lens_core.LedgerWindow(self.root, max_bytes=4096)
        return path, window

    def test_a_line_longer_than_the_refresh_cap_is_discarded_and_counted(self) -> None:
        path, window = self._cold_tail_inside_a_line()
        result = window.refresh()
        # A whole refresh budget with no newline in it: waiting could never
        # complete this line, so it is dropped under a stated policy — counted,
        # its bytes disclosed, and NEVER handed to the parser in pieces.
        self.assertEqual(result["discarded_oversize_lines"], 1)
        self.assertEqual(result["malformed_lines"], 0)
        self.assertEqual(result["pending_tail_bytes"], 0)
        self.assertGreater(result["omitted_prefix_bytes"], 4096)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 0)

        # The rest of the discarded line arrives with its terminator, followed by
        # a good row. The discard does not become a malformed count, is not
        # counted a second time, and the good row still lands.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('"}\n')
            handle.write(json.dumps({"seq": 3, "attempt_id": "a3", "state": "settled",
                                     "prompt_tokens": 2000}) + "\n")
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 0)
        self.assertEqual(result["discarded_oversize_lines"], 1)
        snapshot = window.snapshot()
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)
        self.assertEqual(snapshot["points"][0]["prompt_tokens"], 2000)

    def test_an_unterminated_line_is_retried_not_counted_malformed(self) -> None:
        path, window = self._cold_tail_inside_a_line()
        window.refresh()

        # Still mid-line, and this time the new bytes fit well inside the cap:
        # they are a torn tail, so the offset is HELD and the bytes are
        # disclosed exactly as the incremental torn-tail path does.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("y" * 50)
        result = window.refresh()
        self.assertEqual(result["pending_tail_bytes"], 50)
        self.assertEqual(result["malformed_lines"], 0)
        self.assertEqual(result["discarded_oversize_lines"], 1)   # once per line

        # Nothing was consumed, so re-reading the same bytes changes nothing.
        result = window.refresh()
        self.assertEqual(result["pending_tail_bytes"], 50)
        self.assertEqual(result["malformed_lines"], 0)

        # The terminator finally lands; the next row is read exactly once.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('"}\n')
            handle.write(json.dumps({"seq": 3, "attempt_id": "a3", "state": "settled",
                                     "prompt_tokens": 2000}) + "\n")
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 0)
        self.assertEqual(result["pending_tail_bytes"], 0)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 1)

    def test_read_never_exceeds_the_per_refresh_bound(self) -> None:
        ledger = Ledger()
        for index in range(400):
            ledger.attempt("a%d" % index, prompt_tokens=1000,
                           reserved=False, dispatched=False)
        path = ledger.write(self.root)
        self.assertGreater(os.path.getsize(path), 8192)
        window = lens_core.LedgerWindow(self.root, max_bytes=8192)
        before = window.refresh()
        self.assertLessEqual(before["omitted_prefix_bytes"] + 8192, os.path.getsize(path) + 8192)
        self.assertEqual(before["max_bytes_per_refresh"], 8192)

    def test_replaced_ledger_is_detected_and_re_read(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        path = ledger.write(self.root)
        window = lens_core.LedgerWindow(self.root)
        window.refresh()
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 1)

        # Compaction replaces the file atomically: a new inode at the same path.
        replacement = Ledger()
        replacement.add(kind="usage_baseline", attempt_id="baseline-1", state="settled",
                        baseline_id="baseline-1", compaction_epoch=1,
                        archive_rel="archive/usage_ledger/segment-1.jsonl",
                        folded_attempt_count=41, group_count=1)
        replacement.add(kind="usage_baseline_group", attempt_id="baseline-1-g0000",
                        state="settled", baseline_id="baseline-1",
                        folded_attempt_count=41, prompt_tokens=9_000_000,
                        model="vendor/model-a", category="task")
        replacement.attempt("b1", prompt_tokens=5000)
        other = tempfile.mkdtemp(prefix="context-lens-swap-")
        self.addCleanup(shutil.rmtree, other, True)
        replacement.write(other)
        os.replace(os.path.join(other, "state", "usage_attempts.jsonl"), path)

        result = window.refresh()
        self.assertEqual(result["rotations_observed"], 1)
        self.assertEqual(result["compaction_epoch"], 1)
        self.assertEqual(result["compaction_folded_attempts"], 41)
        snapshot = window.snapshot()
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)
        self.assertEqual([point["prompt_tokens"] for point in snapshot["points"]], [5000])

    def test_truncated_file_under_the_same_inode_is_re_read(self) -> None:
        ledger = Ledger()
        for index in range(6):
            ledger.attempt("a%d" % index, prompt_tokens=1000,
                           reserved=False, dispatched=False)
        path = ledger.write(self.root)
        window = lens_core.LedgerWindow(self.root)
        window.refresh()
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 6)

        with open(path, "r+", encoding="utf-8") as handle:
            content = handle.read()
            handle.seek(0)
            handle.truncate()
            handle.write(content.split("\n")[0] + "\n")
        result = window.refresh()
        self.assertEqual(result["rotations_observed"], 1)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 1)

    def test_reader_never_mutates_the_ledger(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        path = ledger.write(self.root)
        with open(path, "rb") as handle:
            before = handle.read()
        stat_before = os.stat(path)
        window = lens_core.LedgerWindow(self.root)
        window.refresh()
        window.refresh(force_cold=True)
        window.snapshot()
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        stat_after = os.stat(path)
        self.assertEqual(stat_before.st_size, stat_after.st_size)
        self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)


class TestExclusions(LensTestCase):
    def _mixed(self) -> Ledger:
        ledger = Ledger()
        ledger.add(kind="usage_baseline", attempt_id="baseline-1", state="settled",
                   baseline_id="baseline-1", compaction_epoch=2,
                   folded_attempt_count=120, group_count=2)
        ledger.add(kind="usage_baseline_group", attempt_id="baseline-1-g0000",
                   state="settled", baseline_id="baseline-1",
                   folded_attempt_count=100, prompt_tokens=50_000_000,
                   model="vendor/model-a", category="task")
        ledger.add(kind="usage_baseline_group", attempt_id="baseline-1-g0001",
                   state="settled", baseline_id="baseline-1",
                   folded_attempt_count=20, prompt_tokens=9_000_000,
                   model="vendor/model-b", category="subagent")
        ledger.add(kind="subscription_session", attempt_id="session-abc", state="settled",
                   model="harness/model", provider="claude-cli",
                   prompt_tokens=7_000_000, completion_tokens=400_000,
                   category="subagent", source="delegated_subagent", task_id="task-9")
        ledger.add(kind="external_unmetered", attempt_id="external-xyz", state="settled",
                   model="", provider="external", prompt_tokens=0, completion_tokens=0,
                   category="external", source="external_skill", task_id="task-9")
        ledger.add(kind="legacy_metadata", attempt_id="legacy-1", state="settled",
                   model="vendor/old", prompt_tokens=1234)
        ledger.add(kind="legacy_delta", attempt_id="legacy-2", state="settled",
                   model="vendor/old", prompt_tokens=99)
        ledger.attempt("a1", prompt_tokens=120000)
        return ledger

    def test_aggregates_never_reach_the_plot(self) -> None:
        window = self.window(self._mixed())
        window.refresh()
        snapshot = window.snapshot()
        self.assertEqual(len(snapshot["points"]), 1)
        self.assertEqual(snapshot["points"][0]["prompt_tokens"], 120000)
        excluded = snapshot["counters"]["excluded"]
        self.assertEqual(excluded["baseline_rows"], 3)
        self.assertEqual(excluded["subscription_sessions"], 1)
        self.assertEqual(excluded["external_unmetered"], 1)
        self.assertEqual(excluded["legacy_rows"], 2)
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)

    def test_folded_attempts_are_counted_once_not_header_plus_groups(self) -> None:
        # The header's folded_attempt_count (120) is the TOTAL for the same
        # attempts its two group rows partition (100 + 20). Adding both would
        # report every folded attempt twice.
        window = self.window(self._mixed())
        window.refresh()
        excluded = window.snapshot()["counters"]["excluded"]
        self.assertEqual(excluded["folded_attempts"], 120)
        self.assertEqual(excluded["folded_attempts_from_headers"], 120)
        self.assertEqual(excluded["folded_attempts_from_groups"], 0)
        self.assertEqual(excluded["baseline_header_rows"], 1)
        self.assertEqual(excluded["baseline_group_rows"], 2)
        self.assertEqual(excluded["baselines_without_header"], 0)

    def test_group_rows_without_their_header_are_counted_and_disclosed(self) -> None:
        # A cold tail read can retain group rows whose header scrolled out of the
        # window. Then the groups are the only basis, and that is disclosed as a
        # lower bound rather than silently reported as the whole fold.
        ledger = Ledger()
        ledger.add(kind="usage_baseline_group", attempt_id="baseline-1-g0000",
                   state="settled", baseline_id="baseline-1",
                   folded_attempt_count=100, prompt_tokens=50_000_000)
        ledger.add(kind="usage_baseline_group", attempt_id="baseline-1-g0001",
                   state="settled", baseline_id="baseline-1",
                   folded_attempt_count=20, prompt_tokens=9_000_000)
        window = self.window(ledger)
        window.refresh()
        excluded = window.snapshot()["counters"]["excluded"]
        self.assertEqual(excluded["folded_attempts"], 120)
        self.assertEqual(excluded["folded_attempts_from_headers"], 0)
        self.assertEqual(excluded["folded_attempts_from_groups"], 120)
        self.assertEqual(excluded["baselines_without_header"], 1)

    def test_two_compactions_are_counted_as_two_disjoint_folds(self) -> None:
        ledger = Ledger()
        for epoch, total in ((1, 30), (2, 12)):
            baseline = "baseline-%d" % epoch
            ledger.add(kind="usage_baseline", attempt_id=baseline, state="settled",
                       baseline_id=baseline, compaction_epoch=epoch,
                       folded_attempt_count=total, group_count=1)
            ledger.add(kind="usage_baseline_group", attempt_id=baseline + "-g0000",
                       state="settled", baseline_id=baseline,
                       folded_attempt_count=total, prompt_tokens=1_000_000)
        window = self.window(ledger)
        window.refresh()
        excluded = window.snapshot()["counters"]["excluded"]
        self.assertEqual(excluded["folded_attempts"], 42)
        self.assertEqual(excluded["baselines_without_header"], 0)

    def test_subscription_totals_do_not_move_the_peak(self) -> None:
        window = self.window(self._mixed())
        window.refresh()
        summary = lens_core.spread(window.snapshot()["points"])
        self.assertEqual(summary["peak"], 120000)

    def test_unknown_kind_is_excluded_rather_than_guessed(self) -> None:
        ledger = Ledger()
        ledger.add(kind="something_new_in_a_later_version", attempt_id="n1",
                   state="settled", prompt_tokens=500000)
        ledger.attempt("a1", prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        self.assertEqual(snapshot["counters"]["excluded"]["unknown_kind"], 1)
        self.assertEqual(len(snapshot["points"]), 1)

    def test_a_malformed_kind_is_not_treated_as_an_attempt(self) -> None:
        # A non-string `kind` is corruption, not the default. Only an absent or
        # empty kind means `attempt`.
        ledger = Ledger()
        ledger.add(kind=17, attempt_id="n1", state="settled", prompt_tokens=500000)
        ledger.add(kind=["attempt"], attempt_id="n2", state="settled", prompt_tokens=400000)
        ledger.attempt("a1", prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        self.assertEqual(snapshot["counters"]["excluded"]["unknown_kind"], 2)
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)
        self.assertEqual([point["prompt_tokens"] for point in snapshot["points"]], [1000])

    def test_a_row_with_no_recognised_state_is_not_an_ordinary_point(self) -> None:
        ledger = Ledger()
        ledger.add(attempt_id="s1", prompt_tokens=900000, model="vendor/model-a")  # no state
        ledger.add(attempt_id="s2", state="teleported", prompt_tokens=800000)      # unknown state
        ledger.attempt("a1", prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        self.assertEqual(snapshot["counters"]["excluded"]["attempts_without_state"], 2)
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)
        self.assertEqual([point["prompt_tokens"] for point in snapshot["points"]], [1000])
        self.assertEqual(snapshot["facets"]["models"], ["vendor/model-a"])
        self.assertEqual(lens_core.spread(snapshot["points"])["peak"], 1000)


class TestRedaction(LensTestCase):
    def test_internal_ids_never_leave_verbatim(self) -> None:
        ledger = Ledger()
        ledger.attempt("attempt-secret-id", task_id="Rewrite the AUTH module please",
                       root_task_id="Rewrite the AUTH module please",
                       parent_task_id="parent secret", prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        blob = json.dumps(window.snapshot())
        self.assertNotIn("Rewrite the AUTH module", blob)
        self.assertNotIn("parent secret", blob)
        point = window.snapshot()["points"][0]
        self.assertTrue(point["task"].startswith("t-"))
        self.assertTrue(point["root"].startswith("r-"))
        self.assertTrue(point["parent"].startswith("p-"))
        self.assertEqual(len(point["task"]), 14)

    def test_task_keys_are_stable_and_distinguishing(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", task_id="alpha", root_task_id="alpha", prompt_tokens=1)
        ledger.attempt("a2", task_id="alpha", root_task_id="alpha", prompt_tokens=2)
        ledger.attempt("a3", task_id="beta", root_task_id="alpha", prompt_tokens=3)
        window = self.window(ledger)
        window.refresh()
        keys = [point["task"] for point in window.snapshot()["points"]]
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])

    def test_odd_label_strings_are_replaced(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", model="model\nwith newline", category="fine_category",
                       source="s" * 400, prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        point = window.snapshot()["points"][0]
        self.assertEqual(point["model"], "other")
        self.assertEqual(point["category"], "fine_category")
        self.assertEqual(point["source"], "other")

    def test_point_shape_is_exactly_the_allowlist(self) -> None:
        ledger = Ledger()
        ledger.add(state="reserved", attempt_id="a1", model="vendor/model-a",
                   provider="openrouter", task_id="t", root_task_id="t",
                   category="task", source="llm",
                   candidate_raw_sha256="b" * 64, candidate_raw_size_bytes=999,
                   reservation_upper_bound_usd=1.25, global_limit_usd=50.0,
                   review_skill="some-skill", prompt_cache_ttl="1h")
        ledger.add(state="settled", attempt_id="a1", model="vendor/model-a",
                   provider="openrouter", task_id="t", root_task_id="t",
                   category="task", source="llm", prompt_tokens=10,
                   cost_usd=0.42, cost_final=True)
        window = self.window(ledger)
        window.refresh()
        point = window.snapshot()["points"][0]
        self.assertEqual(set(point), {
            "id", "seq", "t", "state", "states", "model", "provider", "category",
            "source", "task", "root", "parent", "prompt_tokens", "completion_tokens",
            "cached_tokens", "cache_write_tokens", "mode", "profile", "basis",
            "target_total_tokens", "capacity_total_tokens", "target_miss",
            "auto_pass", "elapsed_sec",
        })
        blob = json.dumps(window.snapshot())
        for leaked in ("cost_usd", "0.42", "reservation_upper_bound_usd",
                       "candidate_raw_sha256", "review_skill", "global_limit_usd"):
            self.assertNotIn(leaked, blob)


class TestHostileValues(LensTestCase):
    """A corrupt row must degrade to "missing", never to a crash or a bad number."""

    def test_oversized_counts_are_reported_as_missing(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=2 ** 70, completion_tokens=-5,
                       cached_tokens=10 ** 400)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        point = snapshot["points"][0]
        self.assertIsNone(point["prompt_tokens"])
        self.assertIsNone(point["completion_tokens"])
        self.assertIsNone(point["cached_tokens"])
        self.assertEqual(snapshot["counters"]["settled_without_tokens"], 1)
        blob = json.dumps(snapshot)              # a route can still answer
        self.assertNotIn("e+", blob)
        self.assertLess(len(blob), 20000)

    def test_a_count_at_the_browser_safe_edge_still_passes(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=2 ** 53 - 1)
        window = self.window(ledger)
        window.refresh()
        self.assertEqual(window.snapshot()["points"][0]["prompt_tokens"], 2 ** 53 - 1)

    def test_unusable_timestamps_are_dropped_not_plotted(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000, ts_final="not a timestamp",
                       ts_reserved="not a timestamp")
        ledger.attempt("a2", prompt_tokens=2000, ts_final="9999-12-31T23:59:59+00:00",
                       ts_reserved="9999-12-31T23:59:00+00:00")
        ledger.attempt("a3", prompt_tokens=3000, ts_final="1970-01-01T00:00:00+00:00",
                       ts_reserved="1970-01-01T00:00:00+00:00")
        window = self.window(ledger)
        window.refresh()
        points = window.snapshot()["points"]
        self.assertEqual([point["t"] for point in points], [None, None, None])
        self.assertEqual([point["prompt_tokens"] for point in points], [1000, 2000, 3000])
        json.dumps(points)

    def test_an_oversized_seq_is_malformed_rather_than_a_record(self) -> None:
        path = os.path.join(self.root, "state", "usage_attempts.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"seq": 2 ** 70, "attempt_id": "huge",
                                     "state": "settled", "prompt_tokens": 1}) + "\n")
            handle.write(json.dumps({"seq": 2, "attempt_id": "ok",
                                     "state": "settled", "prompt_tokens": 2}) + "\n")
        window = lens_core.LedgerWindow(self.root)
        result = window.refresh()
        self.assertEqual(result["malformed_lines"], 1)
        self.assertEqual(window.snapshot()["counters"]["physical_attempts"], 1)

    def test_a_hostile_ts_cannot_raise_out_of_a_projection(self) -> None:
        for value in ("", "Z", "+", "9" * 200, "0000-00-00T00:00:00", None, 17, {"a": 1}):
            self.assertIsNone(lens_core._epoch_ms(value))


class TestReadPathSafety(LensTestCase):
    def test_a_symlinked_ledger_is_refused_not_followed(self) -> None:
        elsewhere = tempfile.mkdtemp(prefix="context-lens-elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        secret = os.path.join(elsewhere, "somewhere_else.jsonl")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"seq": 1, "attempt_id": "x", "state": "settled",
                                     "prompt_tokens": 1}) + "\n")
        state_dir = os.path.join(self.root, "state")
        os.makedirs(state_dir, exist_ok=True)
        try:
            os.symlink(secret, os.path.join(state_dir, "usage_attempts.jsonl"))
        except (OSError, NotImplementedError):        # pragma: no cover
            self.skipTest("this platform does not allow symlinks here")
        window = lens_core.LedgerWindow(self.root)
        with self.assertRaises(lens_core.LensUnavailable) as caught:
            window.refresh()
        self.assertEqual(caught.exception.code, "ledger_not_confined")

    def test_a_symlinked_state_directory_is_refused(self) -> None:
        elsewhere = tempfile.mkdtemp(prefix="context-lens-elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        with open(os.path.join(elsewhere, "usage_attempts.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"seq": 1, "attempt_id": "x", "state": "settled",
                                     "prompt_tokens": 1}) + "\n")
        try:
            os.symlink(elsewhere, os.path.join(self.root, "state"))
        except (OSError, NotImplementedError):        # pragma: no cover
            self.skipTest("this platform does not allow symlinks here")
        window = lens_core.LedgerWindow(self.root)
        with self.assertRaises(lens_core.LensUnavailable) as caught:
            window.refresh()
        self.assertEqual(caught.exception.code, "ledger_not_confined")

    def test_a_non_regular_ledger_is_refused(self) -> None:
        os.makedirs(os.path.join(self.root, "state", "usage_attempts.jsonl"), exist_ok=True)
        window = lens_core.LedgerWindow(self.root)
        with self.assertRaises(lens_core.LensUnavailable) as caught:
            window.refresh()
        self.assertEqual(caught.exception.code, "ledger_not_regular")

    def test_a_fifo_at_the_ledger_path_is_refused_without_blocking(self) -> None:
        # Opening a FIFO read-only blocks until a writer appears, which would
        # park a request thread. O_NONBLOCK lets the regular-file check refuse it.
        if not hasattr(os, "mkfifo"):                 # pragma: no cover
            self.skipTest("this platform has no FIFOs")
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        try:
            os.mkfifo(os.path.join(self.root, "state", "usage_attempts.jsonl"))
        except OSError:                               # pragma: no cover
            self.skipTest("this filesystem does not support FIFOs")
        window = lens_core.LedgerWindow(self.root)

        outcome = []

        def attempt() -> None:
            try:
                window.refresh()
            except lens_core.LensUnavailable as exc:
                outcome.append(exc.code)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "the open blocked on a FIFO")
        self.assertEqual(outcome, ["ledger_not_regular"])

    def test_a_parent_swapped_after_its_descriptor_was_taken_cannot_steer_the_read(self) -> None:
        """The path-confinement race: parents are opened, not re-resolved.

        ``O_NOFOLLOW`` only ever protects the final component, so checking the
        parents by name and then opening by name leaves a window in which a
        racer can swap ``state`` for a symlink into another tree. Opening each
        component relative to a descriptor already held closes it: the swap
        below happens AFTER the ``state`` descriptor exists, and the ledger that
        is read is still the one inside the serving root.
        """
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        ledger.write(self.root)

        elsewhere = tempfile.mkdtemp(prefix="context-lens-elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        decoy = Ledger()
        decoy.attempt("b1", prompt_tokens=9000)
        decoy.write(elsewhere)

        real_open = os.open
        swapped = []

        def racing_open(target, *args, **kwargs):
            descriptor = real_open(target, *args, **kwargs)
            if target == "state" and not swapped:
                swapped.append(True)
                try:
                    os.rename(os.path.join(self.root, "state"),
                              os.path.join(self.root, "moved-away"))
                    os.symlink(os.path.join(elsewhere, "state"),
                               os.path.join(self.root, "state"))
                except (OSError, NotImplementedError):    # pragma: no cover
                    pass
            return descriptor

        real_support = os.supports_dir_fd
        window = lens_core.LedgerWindow(self.root)
        try:
            os.supports_dir_fd = frozenset(set(real_support) | {racing_open})  # type: ignore[assignment]
            os.open = racing_open                        # type: ignore[assignment]
            result = window.refresh()
        finally:
            os.open = real_open                          # type: ignore[assignment]
            os.supports_dir_fd = real_support            # type: ignore[assignment]

        self.assertTrue(swapped, "the ledger is opened relative to a held directory descriptor")
        self.assertEqual(result["malformed_lines"], 0)
        sizes = [point["prompt_tokens"] for point in window.snapshot()["points"]]
        self.assertEqual(sizes, [1000], "the swapped-in tree was never read")

    def test_a_platform_without_dir_fd_support_fails_closed(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        ledger.write(self.root)
        window = lens_core.LedgerWindow(self.root)
        self.assertGreater(window.refresh()["lines_read"], 0)
        window.reset()

        real_support = os.supports_dir_fd
        try:
            os.supports_dir_fd = frozenset()             # type: ignore[assignment]
            with self.assertRaises(lens_core.LensUnavailable) as caught:
                window.refresh()
        finally:
            os.supports_dir_fd = real_support            # type: ignore[assignment]
        # Falling back to a by-name open would reopen the race; refusing is the
        # only answer that keeps the promise this reader makes.
        self.assertEqual(caught.exception.code, "ledger_not_confined")

    def test_a_replacement_between_the_stat_and_the_open_merges_nothing(self) -> None:
        """The rotation race: two generations must never land in one window."""
        ledger = Ledger()
        for index in range(4):
            ledger.attempt("old%d" % index, prompt_tokens=1000 + index,
                           reserved=False, dispatched=False)
        path = ledger.write(self.root)

        replacement = Ledger()
        for index in range(4):
            replacement.attempt("new%d" % index, prompt_tokens=9000 + index,
                                reserved=False, dispatched=False)
        other = tempfile.mkdtemp(prefix="context-lens-swap-")
        self.addCleanup(shutil.rmtree, other, True)
        replacement_path = replacement.write(other)

        real_stat = os.stat
        swapped = []

        def racing_stat(target, *args, **kwargs):
            result = real_stat(target, *args, **kwargs)
            if target == path and not swapped:
                # Between this stat and the open that follows it, compaction
                # replaces the file. The stat the reader holds is now stale.
                swapped.append(True)
                os.replace(replacement_path, path)
            return result

        window = lens_core.LedgerWindow(self.root)
        try:
            os.stat = racing_stat                    # type: ignore[assignment]
            result = window.refresh()
        finally:
            os.stat = real_stat                      # type: ignore[assignment]

        self.assertTrue(swapped)
        self.assertGreaterEqual(result["rotations_observed"], 1)
        sizes = [point["prompt_tokens"] for point in window.snapshot()["points"]]
        self.assertEqual(sizes, [9000, 9001, 9002, 9003])   # one generation only

    def test_projections_are_detached_from_the_live_cache(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000, final="", dispatched=False)
        path = ledger.write(self.root)
        window = lens_core.LedgerWindow(self.root)
        window.refresh()
        held_records = window.records()
        held_snapshot = window.snapshot()
        before_records = json.dumps(held_records, sort_keys=True)
        before_snapshot = json.dumps(held_snapshot["points"], sort_keys=True)

        # The same attempt settles while a projection is still being consumed.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "seq": 2, "kind": "attempt", "attempt_id": "a1", "state": "settled",
                "ts": "2026-09-07T10:00:09+00:00", "prompt_tokens": 55555,
                "model": "vendor/model-z",
            }) + "\n")
        window.refresh()

        self.assertEqual(json.dumps(held_records, sort_keys=True), before_records)
        self.assertEqual(json.dumps(held_snapshot["points"], sort_keys=True), before_snapshot)
        self.assertEqual(window.snapshot()["points"][0]["prompt_tokens"], 55555)


class TestPointIdentity(LensTestCase):
    def test_the_point_id_is_a_digest_of_the_whole_attempt_id(self) -> None:
        shared_prefix = "attempt-" + "0" * 40
        ledger = Ledger()
        ledger.attempt(shared_prefix + "-one", prompt_tokens=1000)
        ledger.attempt(shared_prefix + "-two", prompt_tokens=2000)
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        ids = [point["id"] for point in snapshot["points"]]
        # A raw prefix would have collapsed these two requests into one.
        self.assertNotEqual(ids[0], ids[1])
        for value in ids:
            self.assertRegex(value, r"\Aa-[0-9a-f]{16}\Z")
        self.assertNotIn(shared_prefix, json.dumps(snapshot))

    def test_the_point_id_is_stable_across_refreshes(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        first = window.snapshot()["points"][0]["id"]
        window.refresh(force_cold=True)
        self.assertEqual(window.snapshot()["points"][0]["id"], first)

    def test_an_odd_attempt_id_cannot_reach_the_browser(self) -> None:
        ledger = Ledger()
        ledger.attempt("<script>alert(1)</script> and a task title", prompt_tokens=1000)
        window = self.window(ledger)
        window.refresh()
        blob = json.dumps(window.snapshot())
        self.assertNotIn("alert(1)", blob)
        self.assertNotIn("<", blob)
        self.assertNotIn("task title", blob)


class TestModes(LensTestCase):
    def test_mode_is_max_low_or_unknown_only(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", prompt_tokens=1000, physical_context=FIT_MAX)
        ledger.attempt("a2", prompt_tokens=2000, physical_context=FIT_LOW)
        ledger.attempt("a3", prompt_tokens=3000)  # no fit measurement recorded
        ledger.attempt("a4", prompt_tokens=4000,
                       physical_context=dict(FIT_MAX, rendered_mode="turbo"))
        window = self.window(ledger)
        window.refresh()
        snapshot = window.snapshot()
        modes = [point["mode"] for point in snapshot["points"]]
        self.assertEqual(modes, ["max", "low", None, None])
        self.assertEqual(snapshot["facets"]["modes"], ["max", "low", "unknown"])

    def test_facets_come_from_the_data(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", model="vendor/model-a", category="task",
                       source="llm", prompt_tokens=1)
        ledger.attempt("a2", model="vendor/model-b", category="skill_review",
                       source="review", prompt_tokens=2)
        window = self.window(ledger)
        window.refresh()
        facets = window.snapshot()["facets"]
        self.assertEqual(facets["models"], ["vendor/model-a", "vendor/model-b"])
        self.assertEqual(facets["categories"], ["skill_review", "task"])
        self.assertEqual(facets["sources"], ["llm", "review"])


HOUR_MS = 3600 * 1000


def iso(ms: int) -> str:
    """The exact ISO shape usage_ledger writes, for an explicit instant."""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).isoformat()


class TestHorizon(LensTestCase):
    """The horizon must select honestly: exact cutoff, before the limit, and no
    claim that a requested span was actually observed."""

    ANCHOR = 1788800000000        # a fixed UTC instant; every case anchors here

    def _spaced(self, offsets_minutes, **kwargs) -> Ledger:
        """One settled attempt per offset, in minutes BEFORE the anchor."""
        ledger = Ledger()
        for index, minutes in enumerate(offsets_minutes):
            moment = self.ANCHOR - minutes * 60000
            ledger.attempt(
                "a%02d" % index,
                prompt_tokens=1000 + index,
                ts_reserved=iso(moment - 1000),
                ts_final=iso(moment),
                **kwargs,
            )
        return ledger

    def _snapshot(self, ledger: Ledger, horizon: str, **kwargs):
        window = self.window(ledger)
        window.refresh()
        return window.snapshot(horizon=horizon, anchor_ms=self.ANCHOR, **kwargs)

    # -- the cut itself ---------------------------------------------------

    def test_the_cutoff_is_inclusive_and_exact_to_the_millisecond(self) -> None:
        ledger = Ledger()
        for name, moment in (
            ("older", self.ANCHOR - HOUR_MS - 1),      # 1 ms before the cutoff
            ("edge", self.ANCHOR - HOUR_MS),           # exactly on it
            ("newer", self.ANCHOR - 60000),
        ):
            ledger.attempt(name, prompt_tokens=1000,
                           ts_reserved=iso(moment - 500), ts_final=iso(moment))
        snapshot = self._snapshot(ledger, "1h")
        times = sorted(point["t"] for point in snapshot["points"])
        self.assertEqual(times, [self.ANCHOR - HOUR_MS, self.ANCHOR - 60000])
        horizon = snapshot["horizon"]
        self.assertEqual(horizon["cutoff_ms"], self.ANCHOR - HOUR_MS)
        self.assertEqual(horizon["now_ms"], self.ANCHOR)
        self.assertEqual(horizon["span_ms"], HOUR_MS)
        self.assertEqual(horizon["excluded_older_than_cutoff"], 1)

    def test_every_span_selects_its_own_records(self) -> None:
        ledger = self._spaced([5, 120, 20 * 60, 4 * 24 * 60, 30 * 24 * 60])
        for horizon, expected in (("1h", 1), ("6h", 2), ("24h", 3), ("7d", 4),
                                  ("available", 5)):
            snapshot = self._snapshot(ledger, horizon)
            self.assertEqual(len(snapshot["points"]), expected, horizon)
            self.assertEqual(snapshot["counters"]["physical_attempts"], expected, horizon)

    def test_the_settled_timestamp_decides_not_the_reserved_one(self) -> None:
        # Reserved well before the cutoff, settled inside it: the request is in.
        ledger = Ledger()
        ledger.attempt("slow", prompt_tokens=4000,
                       ts_reserved=iso(self.ANCHOR - 5 * HOUR_MS),
                       ts_final=iso(self.ANCHOR - 60000))
        snapshot = self._snapshot(ledger, "1h")
        self.assertEqual(len(snapshot["points"]), 1)
        self.assertEqual(snapshot["points"][0]["t"], self.ANCHOR - 60000)

    def test_an_unknown_horizon_token_falls_back_to_available(self) -> None:
        for supplied in ("", "  ", "5m", "all", "1H ", None, 7, "<script>", "available"):
            self.assertIn(lens_core.normalize_horizon(supplied), lens_core.HORIZONS)
        self.assertEqual(lens_core.normalize_horizon("nonsense"), "available")
        self.assertEqual(lens_core.normalize_horizon(" 6H "), "6h")
        ledger = self._spaced([5, 20 * 60])
        snapshot = self._snapshot(ledger, "nonsense")
        self.assertEqual(snapshot["horizon"]["selected"], "available")
        self.assertEqual(len(snapshot["points"]), 2)
        self.assertNotIn("nonsense", json.dumps(snapshot))

    # -- horizon before limit --------------------------------------------

    def test_the_horizon_is_applied_before_the_display_limit(self) -> None:
        # Six inside the last hour, six far outside it. A limit of 3 must trim
        # the SELECTED six, never let an out-of-horizon record back in.
        inside = [5, 10, 15, 20, 25, 30]
        outside = [600, 700, 800, 900, 1000, 1100]
        snapshot = self._snapshot(self._spaced(inside + outside), "1h", limit=3)
        self.assertEqual(len(snapshot["points"]), 3)
        self.assertEqual(snapshot["points_omitted"], 3)
        self.assertEqual(snapshot["horizon"]["attempts_selected"], 6)
        self.assertEqual(snapshot["horizon"]["points_sent"], 3)
        cutoff = snapshot["horizon"]["cutoff_ms"]
        for point in snapshot["points"]:
            self.assertGreaterEqual(point["t"], cutoff)

    def test_the_limit_keeps_the_newest_of_the_selection_in_order(self) -> None:
        snapshot = self._snapshot(self._spaced([50, 40, 30, 20, 10]), "1h", limit=2)
        seqs = [point["seq"] for point in snapshot["points"]]
        self.assertEqual(seqs, sorted(seqs))                 # ledger order kept
        times = [point["t"] for point in snapshot["points"]]
        self.assertEqual(times, [self.ANCHOR - 20 * 60000, self.ANCHOR - 10 * 60000])

    def test_points_counters_and_facets_describe_one_selection(self) -> None:
        ledger = Ledger()
        ledger.attempt("recent", model="vendor/model-in", category="task",
                       prompt_tokens=1000, ts_reserved=iso(self.ANCHOR - 400000),
                       ts_final=iso(self.ANCHOR - 300000))
        ledger.attempt("old", model="vendor/model-out", category="consolidation",
                       prompt_tokens=2000, ts_reserved=iso(self.ANCHOR - 9 * HOUR_MS),
                       ts_final=iso(self.ANCHOR - 8 * HOUR_MS))
        snapshot = self._snapshot(ledger, "1h")
        self.assertEqual(snapshot["facets"]["models"], ["vendor/model-in"])
        self.assertEqual(snapshot["facets"]["categories"], ["task"])
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)
        self.assertEqual(snapshot["counters"]["measured"], 1)
        self.assertEqual(len(snapshot["points"]), 1)
        self.assertNotIn("vendor/model-out", json.dumps(snapshot))

    # -- unknown timestamps ----------------------------------------------

    def test_an_unusable_timestamp_is_counted_separately_never_placed(self) -> None:
        ledger = Ledger()
        ledger.attempt("timed", prompt_tokens=1000,
                       ts_reserved=iso(self.ANCHOR - 120000), ts_final=iso(self.ANCHOR - 60000))
        ledger.attempt("untimed", prompt_tokens=2000,
                       ts_reserved="not a timestamp", ts_final="not a timestamp")
        bounded = self._snapshot(ledger, "1h")
        self.assertEqual(len(bounded["points"]), 1)
        self.assertEqual(bounded["horizon"]["unknown_timestamp"], 1)
        self.assertEqual(bounded["horizon"]["unknown_timestamp_kept"], 0)
        self.assertEqual(bounded["counters"]["physical_attempts"], 1)

        # `available` keeps it — it is a real request — but it still has no time.
        every = self._snapshot(ledger, "available")
        self.assertEqual(len(every["points"]), 2)
        self.assertEqual(every["horizon"]["unknown_timestamp"], 1)
        self.assertEqual(every["horizon"]["unknown_timestamp_kept"], 1)
        self.assertIsNone([p for p in every["points"] if p["prompt_tokens"] == 2000][0]["t"])

    def test_a_ledger_of_only_untimed_rows_confirms_no_part_of_a_span(self) -> None:
        ledger = Ledger()
        ledger.attempt("untimed", prompt_tokens=2000,
                       ts_reserved="", ts_final="")
        horizon = self._snapshot(ledger, "24h")["horizon"]
        self.assertIsNone(horizon["observed_from_ms"])
        self.assertFalse(horizon["covers_selected_span"])

    # -- what the span really covered ------------------------------------

    def test_a_span_inside_the_retained_tail_is_reported_as_covered(self) -> None:
        horizon = self._snapshot(self._spaced([5, 30, 200]), "1h")["horizon"]
        self.assertTrue(horizon["covers_selected_span"])
        self.assertEqual(horizon["observed_from_ms"], self.ANCHOR - 200 * 60000)

    def test_a_span_longer_than_the_retained_tail_is_never_claimed(self) -> None:
        # Only 40 minutes of data exists; a 7-day horizon must not imply 7 days.
        horizon = self._snapshot(self._spaced([5, 20, 40]), "7d")["horizon"]
        self.assertFalse(horizon["covers_selected_span"])
        self.assertEqual(horizon["observed_from_ms"], self.ANCHOR - 40 * 60000)
        self.assertEqual(horizon["observed_to_ms"], self.ANCHOR - 5 * 60000)
        self.assertEqual(horizon["excluded_older_than_cutoff"], 0)

    def test_available_answers_none_rather_than_claiming_all_history(self) -> None:
        snapshot = self._snapshot(self._spaced([5, 40]), "available")
        horizon = snapshot["horizon"]
        self.assertIsNone(horizon["covers_selected_span"])
        self.assertIsNone(horizon["cutoff_ms"])
        self.assertIsNone(horizon["span_ms"])
        self.assertEqual(horizon["selected"], "available")
        self.assertEqual(horizon["options"], list(lens_core.HORIZONS))

    def test_a_bounded_read_marks_the_history_as_truncated(self) -> None:
        ledger = Ledger()
        for index in range(400):
            ledger.attempt("a%03d" % index, prompt_tokens=100000 + index,
                           ts_reserved=iso(self.ANCHOR - (400 - index) * 1000),
                           ts_final=iso(self.ANCHOR - (400 - index) * 1000 + 500))
        window = self.window(ledger, max_bytes=8192)
        window.refresh()
        snapshot = window.snapshot(horizon="7d", anchor_ms=self.ANCHOR)
        self.assertGreater(snapshot["window"]["omitted_prefix_bytes"], 0)
        self.assertTrue(snapshot["horizon"]["history_truncated_by_source"])
        self.assertFalse(snapshot["horizon"]["covers_selected_span"])

    def test_a_record_after_the_anchor_is_kept_and_disclosed(self) -> None:
        ledger = Ledger()
        ledger.attempt("ahead", prompt_tokens=1000,
                       ts_reserved=iso(self.ANCHOR + 1000), ts_final=iso(self.ANCHOR + 5000))
        horizon = self._snapshot(ledger, "1h")["horizon"]
        self.assertEqual(horizon["records_selected"], 1)
        self.assertEqual(horizon["ahead_of_anchor"], 1)

    # -- aggregates ------------------------------------------------------

    def test_aggregates_are_horizon_cut_and_still_never_become_points(self) -> None:
        ledger = Ledger()
        ledger.add(kind="subscription_session", attempt_id="session-in", state="settled",
                   model="harness/model", prompt_tokens=7_000_000,
                   ts=iso(self.ANCHOR - 600000))
        ledger.add(kind="subscription_session", attempt_id="session-out", state="settled",
                   model="harness/model", prompt_tokens=9_000_000,
                   ts=iso(self.ANCHOR - 9 * HOUR_MS))
        ledger.add(kind="usage_baseline", attempt_id="baseline-in", state="settled",
                   baseline_id="baseline-in", compaction_epoch=4,
                   folded_attempt_count=42, ts=iso(self.ANCHOR - 300000))
        ledger.attempt("a1", prompt_tokens=120000,
                       ts_reserved=iso(self.ANCHOR - 200000), ts_final=iso(self.ANCHOR - 100000))
        snapshot = self._snapshot(ledger, "1h")
        excluded = snapshot["counters"]["excluded"]
        self.assertEqual(excluded["subscription_sessions"], 1)      # the older one is out
        self.assertEqual(excluded["baseline_rows"], 1)
        self.assertEqual(excluded["folded_attempts"], 42)
        self.assertEqual(len(snapshot["points"]), 1)
        self.assertEqual(snapshot["points"][0]["prompt_tokens"], 120000)
        self.assertEqual(snapshot["counters"]["physical_attempts"], 1)
        # The whole-file facts stay whole-file facts.
        self.assertEqual(snapshot["window"]["compaction_epoch"], 4)

    # -- refresh and rotation --------------------------------------------

    def test_a_refresh_between_two_reads_does_not_double_count_a_horizon(self) -> None:
        ledger = self._spaced([5, 10, 15])
        window = self.window(ledger)
        window.refresh()
        first = window.snapshot(horizon="1h", anchor_ms=self.ANCHOR)
        window.refresh()
        window.refresh(force_cold=True)
        second = window.snapshot(horizon="1h", anchor_ms=self.ANCHOR)
        self.assertEqual(first["counters"]["physical_attempts"],
                         second["counters"]["physical_attempts"])
        self.assertEqual([point["id"] for point in first["points"]],
                         [point["id"] for point in second["points"]])
        self.assertEqual(second["horizon"]["records_selected"], 3)

    def test_an_appended_row_enters_the_horizon_on_the_next_refresh(self) -> None:
        ledger = self._spaced([50])
        window = self.window(ledger)
        window.refresh()
        self.assertEqual(len(window.snapshot(horizon="1h", anchor_ms=self.ANCHOR)["points"]), 1)
        later = Ledger()
        later.attempt("fresh", prompt_tokens=8000,
                      ts_reserved=iso(self.ANCHOR - 2000), ts_final=iso(self.ANCHOR - 1000))
        with open(os.path.join(self.root, "state", "usage_attempts.jsonl"), "a",
                  encoding="utf-8") as handle:
            for row in later.rows:
                handle.write(json.dumps(dict(row, seq=row["seq"] + 100), sort_keys=True) + "\n")
        window.refresh()
        snapshot = window.snapshot(horizon="1h", anchor_ms=self.ANCHOR)
        self.assertEqual(len(snapshot["points"]), 2)
        self.assertEqual(snapshot["horizon"]["records_selected"], 2)

    def test_a_rotation_re_reads_and_the_horizon_sees_one_generation(self) -> None:
        window = self.window(self._spaced([5, 10]))
        window.refresh()
        self.assertEqual(len(window.snapshot(horizon="1h", anchor_ms=self.ANCHOR)["points"]), 2)
        replacement = Ledger()
        replacement.attempt("new-generation", prompt_tokens=4242,
                            ts_reserved=iso(self.ANCHOR - 3000),
                            ts_final=iso(self.ANCHOR - 2000))
        path = os.path.join(self.root, "state", "usage_attempts.jsonl")
        os.replace(replacement.write(os.path.join(self.root, "next")), path)
        window.refresh()
        snapshot = window.snapshot(horizon="1h", anchor_ms=self.ANCHOR)
        self.assertEqual([point["prompt_tokens"] for point in snapshot["points"]], [4242])
        self.assertEqual(snapshot["horizon"]["records_retained"], 1)
        self.assertGreaterEqual(snapshot["window"]["rotations_observed"], 1)

    # -- trajectory ------------------------------------------------------

    def test_the_trajectory_carries_the_same_horizon_and_names_what_is_outside(self) -> None:
        ledger = Ledger()
        for index, minutes in enumerate([600, 500, 30, 10]):
            moment = self.ANCHOR - minutes * 60000
            ledger.attempt("t%d" % index, task_id="root", root_task_id="root",
                           prompt_tokens=1000 * (index + 1),
                           ts_reserved=iso(moment - 1000), ts_final=iso(moment))
        window = self.window(ledger)
        window.refresh()
        key = lens_core._opaque("root", "t-")
        bounded = lens_core.trajectory(window.records(), key, horizon="1h",
                                       anchor_ms=self.ANCHOR)
        sizes = [point["prompt_tokens"] for group in bounded["groups"] for point in group["points"]]
        self.assertEqual(sizes, [3000, 4000])
        self.assertEqual(bounded["own_outside_horizon"], 2)
        self.assertEqual(bounded["horizon"]["selected"], "1h")
        self.assertEqual(bounded["horizon"]["cutoff_ms"], self.ANCHOR - HOUR_MS)

        every = lens_core.trajectory(window.records(), key, horizon="available",
                                     anchor_ms=self.ANCHOR)
        sizes = [point["prompt_tokens"] for group in every["groups"] for point in group["points"]]
        self.assertEqual(sizes, [1000, 2000, 3000, 4000])
        self.assertEqual(every["own_outside_horizon"], 0)
        self.assertIsNone(every["horizon"]["cutoff_ms"])

    def test_an_anchor_is_taken_when_none_is_supplied(self) -> None:
        before = lens_core.now_ms()
        snapshot = self.window(self._spaced([5])).snapshot(horizon="1h")
        after = lens_core.now_ms()
        self.assertGreaterEqual(snapshot["horizon"]["now_ms"], before)
        self.assertLessEqual(snapshot["horizon"]["now_ms"], after)
        self.assertEqual(snapshot["horizon"]["cutoff_ms"],
                         snapshot["horizon"]["now_ms"] - HOUR_MS)


class TestTrajectory(LensTestCase):
    def _tree(self) -> Ledger:
        ledger = Ledger()
        ledger.attempt("a1", task_id="root", root_task_id="root",
                       model="vendor/model-a", category="task", prompt_tokens=10000,
                       ts_final="2026-09-07T10:00:05+00:00")
        ledger.attempt("a2", task_id="root", root_task_id="root",
                       model="vendor/model-a", category="task", prompt_tokens=30000,
                       ts_final="2026-09-07T10:01:05+00:00")
        ledger.attempt("a3", task_id="root", root_task_id="root",
                       model="vendor/model-light", category="consolidation",
                       prompt_tokens=4000, ts_final="2026-09-07T10:02:05+00:00")
        ledger.attempt("a4", task_id="child", root_task_id="root",
                       parent_task_id="root", model="vendor/model-a",
                       category="subagent", prompt_tokens=8000,
                       ts_final="2026-09-07T10:03:05+00:00")
        ledger.attempt("a5", task_id="unrelated", root_task_id="unrelated",
                       model="vendor/model-a", category="task", prompt_tokens=99000,
                       ts_final="2026-09-07T10:04:05+00:00")
        return ledger

    def test_groups_are_split_by_model_and_work_kind(self) -> None:
        window = self.window(self._tree())
        window.refresh()
        records = window.records()
        task_key = lens_core._opaque("root", "t-")
        result = lens_core.trajectory(records, task_key)
        self.assertEqual(len(result["groups"]), 2)
        keys = sorted((group["model"], group["category"]) for group in result["groups"])
        self.assertEqual(keys, [("vendor/model-a", "task"),
                                ("vendor/model-light", "consolidation")])
        main = [g for g in result["groups"] if g["model"] == "vendor/model-a"][0]
        self.assertTrue(main["joined"])
        self.assertEqual([point["prompt_tokens"] for point in main["points"]], [10000, 30000])

    def test_child_tasks_are_related_and_never_joined(self) -> None:
        window = self.window(self._tree())
        window.refresh()
        task_key = lens_core._opaque("root", "t-")
        result = lens_core.trajectory(window.records(), task_key)
        self.assertEqual(len(result["related"]), 1)
        related = result["related"][0]
        self.assertFalse(related["joined"])
        self.assertEqual(related["category"], "subagent")
        self.assertEqual([point["prompt_tokens"] for point in related["points"]], [8000])

    def test_unrelated_tasks_are_absent(self) -> None:
        window = self.window(self._tree())
        window.refresh()
        result = lens_core.trajectory(window.records(), lens_core._opaque("root", "t-"))
        every = [point["prompt_tokens"] for group in result["groups"] + result["related"]
                 for point in group["points"]]
        self.assertNotIn(99000, every)

    def test_unmeasured_attempts_are_not_in_a_trajectory(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", task_id="root", root_task_id="root", prompt_tokens=1000)
        ledger.attempt("a2", task_id="root", root_task_id="root", final="unresolved")
        window = self.window(ledger)
        window.refresh()
        result = lens_core.trajectory(window.records(), lens_core._opaque("root", "t-"))
        self.assertEqual(sum(len(g["points"]) for g in result["groups"]), 1)

    def test_unknown_task_returns_an_empty_shape(self) -> None:
        window = self.window(self._tree())
        window.refresh()
        self.assertEqual(
            lens_core.trajectory(window.records(), ""),
            {"task": "", "groups": [], "related": [], "root": None,
             "horizon": None, "own_outside_horizon": 0},
        )
        empty = lens_core.trajectory(window.records(), "t-000000000000")
        self.assertEqual(empty["groups"], [])
        self.assertEqual(empty["related"], [])

    def test_an_invalid_key_is_answered_empty_and_never_reflected(self) -> None:
        window = self.window(self._tree())
        window.refresh()
        records = window.records()
        for supplied in (
            "<script>alert(1)</script>",
            "t-not-hex-here",
            "T-ABCDEFABCDEF",
            "t-abcdefabcdef ",           # trailing space is stripped, then valid
            "r-abcdefabcdef",            # a root key is not a task key
            "../../etc/passwd",
            "t-abcdefabcdefabcdef",
            None,
            17,
        ):
            result = lens_core.trajectory(records, supplied)
            self.assertEqual(result["groups"], [])
            self.assertEqual(result["related"], [])
            if supplied == "t-abcdefabcdef ":
                self.assertEqual(result["task"], "t-abcdefabcdef")
                continue
            self.assertEqual(result["task"], "")
            self.assertNotIn(str(supplied), json.dumps(result))

    def test_only_settled_measured_and_timed_points_take_part(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", task_id="root", root_task_id="root", prompt_tokens=1000,
                       ts_final="2026-09-07T10:00:05+00:00")
        # Settled with a size but no usable time: it has no place on a time axis.
        ledger.attempt("a2", task_id="root", root_task_id="root", prompt_tokens=2000,
                       ts_final="not a timestamp")
        # Unresolved, and a released chain: neither is a measured request.
        ledger.attempt("a3", task_id="root", root_task_id="root", final="unresolved")
        ledger.attempt("a4", task_id="root", root_task_id="root", final="released",
                       dispatched=False)
        window = self.window(ledger)
        window.refresh()
        result = lens_core.trajectory(window.records(), lens_core._opaque("root", "t-"))
        sizes = [point["prompt_tokens"] for group in result["groups"] for point in group["points"]]
        self.assertEqual(sizes, [1000])

    def test_a_group_is_ordered_by_time_not_by_arrival(self) -> None:
        # Rows are appended in settle order, which is not always time order.
        ledger = Ledger()
        ledger.attempt("late", task_id="root", root_task_id="root", prompt_tokens=3000,
                       ts_reserved="2026-09-07T10:09:00+00:00",
                       ts_final="2026-09-07T10:09:30+00:00")
        ledger.attempt("early", task_id="root", root_task_id="root", prompt_tokens=1000,
                       ts_reserved="2026-09-07T10:01:00+00:00",
                       ts_final="2026-09-07T10:01:30+00:00")
        window = self.window(ledger)
        window.refresh()
        result = lens_core.trajectory(window.records(), lens_core._opaque("root", "t-"))
        points = result["groups"][0]["points"]
        self.assertEqual([point["prompt_tokens"] for point in points], [1000, 3000])
        self.assertLess(points[0]["t"], points[1]["t"])

    def test_records_without_a_task_are_not_merged_into_one_related_group(self) -> None:
        ledger = Ledger()
        ledger.attempt("a1", task_id="root", root_task_id="root", prompt_tokens=1000)
        # Same root, no task id of their own: they cannot be attributed.
        ledger.attempt("a2", task_id="", root_task_id="root", prompt_tokens=2000)
        ledger.attempt("a3", task_id="", root_task_id="root", prompt_tokens=3000)
        window = self.window(ledger)
        window.refresh()
        result = lens_core.trajectory(window.records(), lens_core._opaque("root", "t-"))
        self.assertEqual(result["related"], [])


if __name__ == "__main__":
    unittest.main()
