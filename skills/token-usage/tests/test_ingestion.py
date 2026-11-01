"""Synthetic public journal fixtures; never reads a user's runtime data."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ingestion import RefreshCancelled, UsageIndex


def event(attempt_id, **fields):
    return dict(attempt_id=attempt_id, kind="attempt", state="settled", seq=1,
                ts="2026-08-01T00:00:00Z", **fields)


def journal(*rows):
    return b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "state").mkdir()
        self.path = self.root / "state/usage_attempts.jsonl"
        self.path.write_bytes(b"")
        (self.root / "state/project_task_bindings.json").write_text('{"bindings":{}}')
        (self.root / "state/projects.json").write_text('{"projects":[]}')
        self.index = UsageIndex(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, *rows):
        self.path.write_bytes(journal(*rows))
        return self.index.refresh()

    def archive(self, name, content):
        path = self.root / "archive/usage_ledger" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return dict(kind="usage_baseline", archive_rel="archive/usage_ledger/" + name,
                    source_sha256=hashlib.sha256(content).hexdigest(), source_size_bytes=len(content),
                    source_row_count=len(content.splitlines()), compaction_epoch=name)

    def test_transitions_are_last_line_not_largest_sequence(self):
        reserved = event("a", prompt_tokens=999)
        reserved.update(state="reserved", seq=900)
        settled = event("a", prompt_tokens=0, completion_tokens=None)
        output = self.put(reserved, settled)
        self.assertEqual(len(output["rows"]), 1)
        row = output["rows"][0]
        self.assertEqual(row["prompt_tokens"], 0)
        self.assertIsNone(row["completion_tokens"])
        self.assertIsNone(row["cached_tokens"])
        self.assertNotIn("seq", row)
        self.assertEqual(output["coverage"]["physical_calls"], 1)

    def test_legacy_and_native_numbers_are_never_renormalized(self):
        output = self.put(event("legacy", prompt_tokens=15, cached_tokens=3037316,
                                cache_write_tokens=448991, completion_tokens=39828,
                                source="claude_code.readonly"),
                          event("native", prompt_tokens=120, cached_tokens=80,
                                cache_write_tokens=10, completion_tokens=5))
        self.assertEqual(output["rows"][0]["prompt_tokens"], 15)
        self.assertEqual(output["rows"][0]["cached_tokens"], 3037316)
        self.assertEqual(output["rows"][1]["prompt_tokens"], 120)

    def test_subscription_sessions_are_aggregate_and_unknown(self):
        session = event("s", prompt_tokens=None, completion_tokens=None, session_id_sha256="not-retained")
        session["kind"] = "subscription_session"
        raw = event("raw", prompt_tokens=20, completion_tokens=1)
        output = self.put(session, raw)
        self.assertEqual(output["coverage"]["session_aggregates"], 1)
        self.assertEqual(output["coverage"]["physical_calls"], 1)
        self.assertIsNone(output["rows"][0]["prompt_tokens"])
        self.assertNotIn("session_id_sha256", output["rows"][0])

    def test_normalized_input_usage_exact_contract_independent_of_legacy(self):
        valid = {"total_tokens": 2 ** 60 + 1, "cache_read_tokens": 0,
                 "cache_write_tokens": None}
        all_null = dict.fromkeys(valid)
        all_zero = dict.fromkeys(valid, 0)
        cases = [None, {}, {"total_tokens": 3}, dict(valid, extra=0),
                 [], "unavailable"]
        for field in valid:
            for bad in (True, False, -1, 1.0, "1", [], {}):
                cases.append(dict(valid, **{field: bad}))
        for number, value in enumerate(cases + [valid, all_null, all_zero]):
            with self.subTest(value=value):
                session = event("s", input_token_usage=value, prompt_tokens=23,
                                cached_tokens=17)
                session["kind"] = "subscription_session"
                row = self.put(session)["rows"][0]
                self.assertEqual(row["prompt_tokens"], 23)
                self.assertEqual(row["cached_tokens"], 17)
                self.assertEqual(row["input_token_usage"],
                                 None if number < len(cases) else value)
        session = event("independent", input_token_usage=valid)
        session["kind"] = "subscription_session"
        row = self.put(session)["rows"][0]
        self.assertIsNone(row["prompt_tokens"])
        self.assertIsNone(row["cached_tokens"])
        self.assertEqual(row["input_token_usage"], valid)
        row["input_token_usage"]["total_tokens"] = 7
        self.assertEqual(self.index.snapshot()["rows"][0]["input_token_usage"], valid)
        self.assertIsNone(self.put(event("absent"))["rows"][0]["input_token_usage"])

    def test_session_lifecycle_replaces_whole_normalized_object(self):
        first = event("session", input_token_usage={"total_tokens": 90,
                      "cache_read_tokens": 40, "cache_write_tokens": 5})
        first.update(kind="subscription_session", seq=999)
        latest = dict(first, seq=1, input_token_usage={"total_tokens": 100})
        result = self.put(first, latest)
        self.assertEqual(result["coverage"]["session_aggregates"], 1)
        self.assertIsNone(result["rows"][0]["input_token_usage"])

    def test_append_torn_line_and_unchanged_zero_bytes(self):
        self.put(event("a", prompt_tokens=1))
        before = self.index.snapshot()["snapshot_id"]
        unchanged = self.index.refresh()
        self.assertEqual(unchanged["coverage"]["source_bytes_read"], 0)
        self.assertEqual(unchanged["snapshot_id"], before)
        content = journal(event("b", prompt_tokens=2))
        with self.path.open("ab") as stream:
            stream.write(content[:25])
        partial = self.index.refresh()
        self.assertEqual(len(partial["rows"]), 1)
        self.assertEqual(partial["coverage"]["pending_partial_bytes"], 25)
        with self.path.open("ab") as stream:
            stream.write(content[25:])
        complete = self.index.refresh()
        self.assertEqual(len(complete["rows"]), 2)
        self.assertEqual(complete["coverage"]["pending_partial_bytes"], 0)
        self.assertEqual(complete["coverage"]["source_rows"], 2)

    def test_malformed_oversize_lines_are_visible_and_skipped(self):
        self.path.write_bytes(b"oops\n" + b"x" * 201 + b"\n" + journal(event("good")))
        with patch("ingestion.MAX_LINE", 200):
            output = self.index.refresh()
        self.assertEqual(output["coverage"]["malformed_lines"], 1)
        self.assertEqual(output["coverage"]["oversize_lines"], 1)
        self.assertEqual([r["attempt_id"] for r in output["rows"]], ["good"])
        self.assertFalse(output["coverage"]["history_complete"])

    def test_truncation_and_replacement_do_not_resurrect(self):
        self.put(event("old-a"), event("old-b"))
        self.put(event("new"))
        self.assertEqual([r["attempt_id"] for r in self.index.snapshot()["rows"]], ["new"])
        replacement = self.root / "replacement"
        replacement.write_bytes(journal(event("replacement"), event("another")))
        os.replace(replacement, self.path)
        self.assertEqual([r["attempt_id"] for r in self.index.refresh()["rows"]], ["replacement", "another"])

    def test_multi_epoch_archives_dedup_and_sequence_resets(self):
        first = event("a", prompt_tokens=1)
        first["seq"] = 999
        h1 = self.archive("first.jsonl", journal(first, event("b", prompt_tokens=2)))
        h2 = self.archive("second.jsonl", journal(h1, event("a", prompt_tokens=3),
                                                {"kind": "usage_baseline_group", "prompt_tokens": 9999}))
        output = self.put(h2, event("a", prompt_tokens=4), event("c", prompt_tokens=5))
        self.assertEqual({r["attempt_id"]: r["prompt_tokens"] for r in output["rows"]}, {"a": 4, "b": 2, "c": 5})
        self.assertEqual(output["coverage"]["archive_segments"], 2)
        self.assertTrue(output["coverage"]["history_complete"])
        self.assertEqual(self.index.refresh()["coverage"]["source_bytes_read"], 0)

    def test_unchanged_refresh_does_not_read_archive_or_live_payload(self):
        from contextlib import contextmanager
        header = self.archive("full.jsonl", journal(*(event(str(i)) for i in range(100))))
        self.put(header, event("current"))
        opened = []
        reads = []
        safe_open = self.index._safe_open

        class Measured:
            def __init__(self, stream, path):
                self.stream, self.path = stream, path

            def read(self, size=-1):
                data = self.stream.read(size)
                reads.append((self.path, len(data)))
                return data

            def readline(self, size=-1):
                data = self.stream.readline(size)
                reads.append((self.path, len(data)))
                return data

            def __getattr__(self, name):
                return getattr(self.stream, name)

        @contextmanager
        def measured_open(path):
            opened.append(path)
            with safe_open(path) as stream:
                yield Measured(stream, path)

        with patch.object(self.index, "_safe_open", measured_open):
            result = self.index.refresh()
        self.assertIn(self.path, opened)  # File identity checks still occur.
        self.assertIn(self.root / header["archive_rel"], opened)
        self.assertEqual(reads, [])
        self.assertEqual(result["coverage"]["source_bytes_read"], 0)

    def test_archive_row_count_and_partial_tail_fail_closed(self):
        for mode in ("count", "partial", "hash"):
            with self.subTest(mode=mode):
                content = journal(event("untrusted"))
                if mode == "partial":
                    content = content[:-1]
                header = self.archive("check.jsonl", content)
                if mode == "count":
                    header["source_row_count"] += 1
                if mode == "hash":
                    header["source_sha256"] = "0" * 64
                result = self.put(header, event("current"))
                self.assertEqual([r["attempt_id"] for r in result["rows"]], ["current"])
                self.assertFalse(result["coverage"]["history_complete"])

    def test_missing_and_tampered_archive_is_partial_not_empty_success(self):
        header = self.archive("a.jsonl", journal(event("old")))
        output = self.put(header, event("current"))
        self.assertEqual(len(output["rows"]), 2)
        path = self.root / header["archive_rel"]
        path.write_bytes(journal(event("evil")))
        output = self.index.refresh()
        self.assertEqual([r["attempt_id"] for r in output["rows"]], ["current"])
        self.assertFalse(output["coverage"]["history_complete"])
        path.unlink()
        output = self.index.refresh()
        self.assertEqual(output["coverage"]["status"], "partial")

    def test_missing_older_archive_keeps_verified_newer_history(self):
        h1 = self.archive("first.jsonl", journal(event("old")))
        h2 = self.archive("second.jsonl", journal(h1, event("middle")))
        (self.root / h1["archive_rel"]).unlink()
        output = self.put(h2, event("current"))
        self.assertEqual({r["attempt_id"] for r in output["rows"]}, {"middle", "current"})
        self.assertFalse(output["coverage"]["history_complete"])

    def test_unlinked_archive_is_never_authority(self):
        self.archive("unlinked.jsonl", journal(event("must-not-exist")))
        self.assertEqual([r["attempt_id"] for r in self.put(event("live"))["rows"]], ["live"])

    def test_traversal_absolute_and_symlink_archives_rejected(self):
        for path in ("../outside", "/absolute", "archive/usage_ledger/../escape", "archive//usage_ledger/a"):
            with self.subTest(path=path):
                header = self.archive("valid.jsonl", journal(event("old")))
                header["archive_rel"] = path
                output = self.put(header, event("current"))
                self.assertEqual(len(output["rows"]), 1)
                self.assertFalse(output["coverage"]["history_complete"])
        header = self.archive("link.jsonl", journal(event("old")))
        path = self.root / header["archive_rel"]
        path.unlink()
        path.symlink_to(self.path)
        self.assertFalse(self.put(header, event("current"))["coverage"]["history_complete"])

    def test_source_symlink_and_missing_are_errors(self):
        target = self.root / "safe-local-target"
        target.write_bytes(journal(event("not-read")))
        self.path.unlink()
        self.path.symlink_to(target)
        self.assertEqual(self.index.refresh()["coverage"]["status"], "error")
        self.path.unlink()
        self.assertEqual(self.index.refresh()["coverage"]["status"], "error")

    def test_symlinked_data_root_and_archive_parent_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(UsageIndex(alias).refresh()["coverage"]["status"], "error")
        header = self.archive("a.jsonl", journal(event("old")))
        archive_dir = self.root / "archive/usage_ledger"
        archive_dir.rename(self.root / "other-ledger")
        archive_dir.symlink_to(self.root / "other-ledger", target_is_directory=True)
        output = self.put(header, event("current"))
        self.assertEqual([r["attempt_id"] for r in output["rows"]], ["current"])
        self.assertFalse(output["coverage"]["history_complete"])

    def test_exact_large_integer_and_invalid_numbers(self):
        huge = 2 ** 60 + 1
        output = self.put(event("large", prompt_tokens=huge, completion_tokens=False,
                                cached_tokens=-1, cache_write_tokens=0, cost_usd=float("inf")))
        row = output["rows"][0]
        self.assertEqual(row["prompt_tokens"], huge)
        self.assertIsNone(row["completion_tokens"])
        self.assertIsNone(row["cached_tokens"])
        self.assertEqual(row["cache_write_tokens"], 0)
        self.assertIsNone(row["cost_usd"])

    def test_same_size_rewrites_replace_journal_and_metadata(self):
        self.put(event("old", prompt_tokens=1))
        self.put(event("new", prompt_tokens=2))
        self.assertEqual([r["attempt_id"] for r in self.index.snapshot()["rows"]], ["new"])
        path = self.root / "state/project_task_bindings.json"
        path.write_text('{"bindings":{"task":{"project_id":"p"}}}')
        first = self.put(event("a", task_id="task"))
        path.write_text('{"bindings":{"task":{"project_id":"q"}}}')
        second = self.index.refresh()
        self.assertEqual(first["rows"][0]["project_id"], "p")
        self.assertEqual(second["rows"][0]["project_id"], "q")
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])

    def test_narrow_metadata_root_join_conflicts_and_private_fields(self):
        bindings = {"bindings": {
            "root": {"task_id": "root", "project_id": "p", "source_text": "PRIVATE " * 20000},
            "child-b": {"task_id": "child-b", "project_id": "q"}}}
        (self.root / "state/project_task_bindings.json").write_text(json.dumps(bindings))
        (self.root / "state/projects.json").write_text(json.dumps({"projects": [{"id": "p", "name": "Project P"}, {"id": "q", "name": "Project Q"}]}))
        output = self.put(event("a", task_id="root", root_task_id="root"),
                          event("b", task_id="child-a", root_task_id="root"),
                          event("c", task_id="child-b", root_task_id="root"),
                          event("d", task_id="orphan", parent_task_id="root"))
        self.assertEqual([r["project_assignment"] for r in output["rows"]], ["direct", "root", "conflict", "unassigned"])
        self.assertEqual([r["project_id"] for r in output["rows"]], ["p", "p", "q", None])
        self.assertEqual(output["coverage"]["project_conflicts"], 1)
        self.assertEqual(output["coverage"]["project_gaps"], 1)
        self.assertNotIn("PRIVATE", repr(self.index.__dict__))
        self.assertNotIn("source_text", repr(output))

    def test_invalid_metadata_is_visible_no_partial_join(self):
        (self.root / "state/project_task_bindings.json").write_text('{"bindings":{"r":{"project_id":"p"}},"bad":')
        output = self.put(event("a", task_id="r"))
        self.assertIsNone(output["rows"][0]["project_id"])
        self.assertEqual(output["coverage"]["status"], "partial")
        self.assertTrue(output["coverage"]["metadata"]["issues"])

    def test_snapshot_defensive_copy_and_concurrent_refresh(self):
        self.put(event("a", prompt_tokens=0))
        errors = []
        def poll():
            try:
                for _ in range(10):
                    result = self.index.refresh()
                    self.assertEqual(result["rows"][0]["prompt_tokens"], 0)
                    result["rows"][0]["prompt_tokens"] = 999
                    result["coverage"]["issues"].append("external mutation")
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=poll) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertNotIn("external mutation", self.index.snapshot()["coverage"]["issues"])

    def test_cancelled_catchup_keeps_last_snapshot_and_can_resume(self):
        stop = threading.Event()
        self.index = UsageIndex(self.root, stop_event=stop)
        previous = self.put(event("previous"))
        self.path.write_bytes(journal(*(event(str(i)) for i in range(100))))
        original_check = self.index._check_cancel
        calls = 0

        def check():
            nonlocal calls
            calls += 1
            if calls == 10:
                stop.set()
            original_check()

        with patch.object(self.index, "_check_cancel", check):
            with self.assertRaises(RefreshCancelled):
                self.index.refresh()
        self.assertLess(calls, 100)
        self.assertEqual(self.index.snapshot(), previous)
        stop.clear()
        resumed = self.index.refresh()
        self.assertEqual(len(resumed["rows"]), 100)
        self.assertTrue(resumed["coverage"]["history_complete"])

    def test_cancelled_large_metadata_scan_keeps_last_bindings(self):
        stop = threading.Event()
        self.index = UsageIndex(self.root, stop_event=stop)
        previous = self.put(event("previous"))
        path = self.root / "state/project_task_bindings.json"
        path.write_text(json.dumps({"bindings": {}, "source_text": "private" * 10000}))
        original_check = self.index._check_cancel
        calls = 0

        def check():
            nonlocal calls
            calls += 1
            if calls == 5:
                stop.set()
            original_check()

        with patch.object(self.index, "_check_cancel", check):
            with self.assertRaises(RefreshCancelled):
                self.index.refresh()
        self.assertEqual(self.index.snapshot(), previous)


if __name__ == "__main__":
    unittest.main()
