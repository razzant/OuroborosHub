import asyncio
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from starlette.requests import Request

PAYLOAD = Path(__file__).resolve().parent.parent


def load_payload(package_name):
    """Load ``plugin.py`` the way the host extension loader does.

    The payload directory is never added to ``sys.path``. The entry file is
    given a unique package name whose ``__path__`` is the payload directory,
    and the module is registered in ``sys.modules`` *before* it is executed, so
    its relative sibling imports resolve while it is still running. Testing
    through a ``sys.path`` shim instead would pass for an entry file that the
    live loader cannot import.
    """
    spec = importlib.util.spec_from_file_location(
        package_name, PAYLOAD / "plugin.py",
        submodule_search_locations=[str(PAYLOAD)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(package_name, None)
        raise
    return module, sys.modules[package_name + ".memory_reader"]


plugin_module, memory_reader = load_payload("memory_atlas_payload_under_test")
AtlasError = memory_reader.AtlasError
MAX_FILE = memory_reader.MAX_FILE
MAX_GRAPH_EDGES = memory_reader.MAX_GRAPH_EDGES
MAX_CURSOR_CHARS = memory_reader.MAX_CURSOR_CHARS
MAX_SEARCH_OFFSET = memory_reader.MAX_SEARCH_OFFSET
MemoryReader = memory_reader.MemoryReader
Source = memory_reader.Source
MemoryAtlasPlugin = plugin_module.MemoryAtlasPlugin


class LoaderTests(unittest.TestCase):
    def test_entry_imports_its_sibling_relatively(self):
        text = (PAYLOAD / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("from .memory_reader import", text)
        for absolute in ("\nfrom memory_reader import", "\nimport memory_reader"):
            self.assertNotIn(absolute, text)

    def test_payload_loads_without_the_payload_directory_on_sys_path(self):
        name = "memory_atlas_payload_probe"
        saved = list(sys.path)
        sys.path[:] = [entry for entry in sys.path
                       if Path(entry or ".").resolve() != PAYLOAD]
        try:
            module, reader_module = load_payload(name)
            self.assertTrue(callable(module.register))
            self.assertIs(reader_module.MemoryReader, reader_module.MemoryReader)
            self.assertEqual(reader_module.__name__, name + ".memory_reader")
        finally:
            sys.path[:] = saved
            sys.modules.pop(name + ".memory_reader", None)
            sys.modules.pop(name, None)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "memory/knowledge").mkdir(parents=True)
        (self.root / "projects/alpha/knowledge").mkdir(parents=True)
        self.write("memory/identity.md", "Straße <script> café")
        self.write("memory/scratchpad.md", "scratch sharedterm")
        self.write("memory/knowledge/facts.md",
                   "facts knowledge:patterns sharedterm identity scratchpad "
                   "[see](patterns.md) "
                   "[[patterns]] [proj](../../projects/alpha/knowledge/notes.md) "
                   "[abs](/etc/passwd) [web](https://example.com/x.md) "
                   "[outside](../WORLD.md)")
        self.write("memory/knowledge/patterns.md", "patterns sharedterm")
        self.write("memory/knowledge/improvement-backlog.md", "backlog item")
        self.write("memory/knowledge/index-full.md", "derived")
        self.write("memory/WORLD.md", "generated world profile")
        self.write("memory/registry.md", "registry rows")
        self.write("memory/deep_review.md", "latest self review")
        self.write("memory/dialogue_summary.md", "legacy dialogue summary")
        self.write("memory/dialogue_blocks.json", json.dumps([
            {"ts": "1", "type": "summary", "content": "summary one",
             "range": [0, 100], "message_count": 100},
            {"ts": "2", "type": "era", "content": "era block",
             "range": [100, 400], "message_count": 300},
            {"ts": "3", "type": "gap", "content": "gap block", "gap_id": "g-1"},
            {"ts": "4", "type": "banana", "content": "unrecognised type"},
            {"ts": "5", "content": "no type at all"},
            {"ts": "6", "type": "summary", "content": "x" * 9000},
            {"ts": "7", "type": "summary", "content": 42},
            "not-an-object"]))
        self.write("memory/dialogue_meta.json", json.dumps(
            {"last_consolidated_offset": 600, "last_consolidated_at": "2026-09-01",
             "chat_log_signature": {"path": "logs/chat.jsonl", "lines": 600,
                                    "generation": {"first_line_sha256": "abc"}}}))
        self.write("logs/task_reflections.jsonl", "".join((
            self.jline({"ts": "10", "task_id": "t", "task_type": "build",
                        "goal": "ship it", "rounds": 3, "cost_usd": 0.5,
                        "error_count": 1, "key_markers": ["k"],
                        "review_evidence": "e", "reflection": "went fine",
                        "backlog_candidates": [], "memory_actions": []}),
            self.jline({"ts": "11", "task_id": "t2",
                        "type": "project_reflection_pointer",
                        "project_id": "alpha",
                        "reflection_path": "projects/alpha/logs/task_reflections.jsonl"}))))
        self.write("projects/alpha/logs/task_reflections.jsonl", self.jline(
            {"ts": "11", "task_id": "t2", "task_type": "fix", "goal": "repair",
             "rounds": 1, "cost_usd": 0.1, "error_count": 0,
             "reflection": "project reflection body"}))
        self.write("projects/alpha/knowledge/notes.md", "project notes")
        self.write("projects/alpha/knowledge/index-full.md", "project derived")
        self.write("projects/alpha/workpad.md", "workpad")
        self.write("projects/alpha/journal.jsonl", self.jline(
            {"ts": "8", "kind": "note", "text": "journal", "task_id": "t"}))
        self.write("memory/identity_journal.jsonl", "".join((
            self.jline({"ts": "1", "source_type": "edit", "old_content": "OLD café",
                        "new_content": "NEW Straße", "old_sha256": "a", "new_sha256": "b"}),
            self.jline({"ts": "2", "source_type": "digest", "content_digested": True,
                        "digest_preview": "digest", "old_preview": "before",
                        "new_preview": "after", "old_sha256": "b", "new_sha256": "c"}),
            "not-json\n")))
        self.write("memory/scratchpad_blocks.json", json.dumps([
            {"ts": "3", "source": "user", "content": "complete block", "metadata": {}},
            {"ts": "3.1", "source": "user"}]))
        # Exactly the record shapes the native scratchpad writer emits: an
        # append with a nested block, an eviction with flat evicted_block_*
        # fields, a failed append whose block was never stored, and a
        # well-formed record type this reader does not know.
        self.write("memory/scratchpad_journal.jsonl", "".join((
            self.jline({"ts": "4", "type": "block_appended", "content_len": 17,
                        "source": "user", "metadata": {"tag": "note"},
                        "block": {"ts": "4", "source": "user",
                                  "content": "appended complete"}}),
            self.jline({"ts": "5", "type": "block_evicted", "evicted_block_ts": "3",
                        "evicted_block_source": "user",
                        "evicted_block_content": "retired complete",
                        "source_ref": {"kind": "task", "id": "t-1"}}),
            self.jline({"ts": "6", "type": "block_append_failed", "source": "system",
                        "block": {"ts": "6", "source": "system",
                                  "content": "never stored"}}),
            self.jline({"ts": "7", "type": "scratchpad_trimmed", "source": "system"}),
            # Recorded provenance: one read of a catalogued source, one of a
            # path outside the root, and one shared task_id.
            self.jline({"ts": "8", "type": "scratchpad_read", "task_id": "t",
                        "source_ref": {"entry_id": "e-1", "read": {"arguments": {
                            "path": "memory/knowledge/patterns.md"}}}}),
            self.jline({"ts": "9", "type": "scratchpad_read",
                        "source_ref": {"read": {"arguments": {
                            "path": "../../etc/passwd"}}}}))))
        self.write("memory/knowledge_history.jsonl", "".join((
            self.jline({"ts": "4", "topic": "facts", "old_content": "old facts",
                        "new_content": "new facts", "old_sha256": "1", "new_sha256": "2",
                        "mode": "replace"}),
            self.jline({"ts": "4.5", "topic": "facts", "source_type": "digest",
                        "content_digested": True, "digest_preview": "facts digest",
                        "old_preview": "old p", "new_preview": "new p",
                        "old_sha256": "2", "new_sha256": "3"}),
            self.jline({"ts": "4", "topic": "patterns", "old_content": "old patterns",
                        "new_content": "new patterns"}))))
        self.write("memory/knowledge_journal.jsonl", self.jline(
            {"ts": "5", "topic": "facts", "kind": "updated", "mode": "replace"}))
        self.write("memory/knowledge/patterns_history.jsonl", self.jline(
            {"ts": "5", "old_content": "special old", "new_content": "special new"}))
        self.write("projects/alpha/knowledge_history.jsonl", self.jline(
            {"ts": "6", "topic": "notes", "old_content": "project old",
             "new_content": "project new"}))
        self.write("projects/alpha/knowledge_journal.jsonl", self.jline(
            {"ts": "7", "topic": "notes", "kind": "changed"}))
        self.reader = MemoryReader(str(self.root))

    def tearDown(self):
        self.reader.close()
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def jline(value):
        return json.dumps(value, ensure_ascii=False) + "\n"

    def test_catalog_families_exclusions_and_unavailable_history(self):
        result = self.reader.catalog()
        ids = [item["id"] for item in result["data"]["items"]]
        self.assertEqual(ids, sorted(ids))
        self.assertTrue({"identity", "scratchpad", "knowledge:facts", "knowledge:patterns",
                         "project:alpha:knowledge:notes", "project:alpha:workpad",
                         "project:alpha:journal"}.issubset(ids))
        self.assertNotIn("knowledge:index-full", ids)
        self.assertNotIn("project:alpha:knowledge:index-full", ids)
        self.assertTrue(any(g["reason"] == "derived_excluded" and
                            "index-full.md" in g["scope"] for g in result["gaps"]))
        self.assertGreaterEqual(sum(1 for g in result["gaps"]
                                    if g["reason"] == "derived_excluded"), 2)

    NEW_SOURCES = {
        "dialogue": ("dialogue", "memory/dialogue_blocks.json", "none"),
        "dialogue_legacy": ("dialogue_legacy", "memory/dialogue_summary.md", "none"),
        "world": ("world", "memory/WORLD.md", "none"),
        "registry": ("registry", "memory/registry.md", "none"),
        "deep_review": ("deep_review", "memory/deep_review.md", "none"),
        "reflections": ("reflections", "logs/task_reflections.jsonl", "activity"),
        "project:alpha:reflections": ("project_reflections",
                                      "projects/alpha/logs/task_reflections.jsonl",
                                      "activity"),
    }

    def test_new_sources_are_discovered_readable_and_correctly_shaped(self):
        items = {i["id"]: i for i in self.reader.catalog(limit=200)["data"]["items"]}
        for sid, (family, rel, history) in self.NEW_SOURCES.items():
            self.assertIn(sid, items, sid)
            self.assertEqual(items[sid]["family"], family, sid)
            self.assertEqual(items[sid]["history"], history, sid)
            self.assertIsNone(items[sid]["read_error"], sid)
            # Reachable through the no-follow descriptor read path, which
            # validates every path part on the way down.
            data = self.reader.document(sid)["data"]
            self.assertEqual(data["content"],
                             (self.root / rel).read_text(encoding="utf-8"), sid)
        self.assertEqual(items["dialogue"]["title"], "Dialogue chronicle")
        self.assertEqual(items["reflections"]["title"], "Task reflections")
        self.assertEqual(items["project:alpha:reflections"]["title"],
                         "alpha / task reflections")
        # dialogue_meta.json is provenance, not a catalogued document.
        self.assertNotIn("dialogue_meta", items)
        self.assertFalse(any("dialogue_meta" in i for i in items))

    def test_missing_optional_source_is_simply_absent(self):
        (self.root / "memory/dialogue_summary.md").unlink()
        (self.root / "memory/registry.md").unlink()
        result = self.reader.catalog(limit=200)
        ids = [i["id"] for i in result["data"]["items"]]
        self.assertNotIn("dialogue_legacy", ids)
        self.assertNotIn("registry", ids)
        self.assertIn("world", ids)
        self.assertFalse(any(g["scope"] in ("dialogue_legacy", "registry")
                             for g in result["gaps"]))

    def test_compare_supported_is_false_only_for_improvement_backlog(self):
        items = self.reader.catalog(limit=200)["data"]["items"]
        false_ids = [i["id"] for i in items if i["compare_supported"] is False]
        self.assertEqual(false_ids, ["knowledge:improvement-backlog"])
        # patterns stays an ordinary comparable knowledge topic.
        patterns = next(i for i in items if i["id"] == "knowledge:patterns")
        self.assertTrue(patterns["compare_supported"])
        self.assertEqual(patterns["history"], "mixed")

    def test_reflection_full_rows_and_pointer_rows_stay_distinguishable(self):
        items = self.reader.history("reflections")["data"]["items"]
        self.assertEqual([i["representation"] for i in items],
                         ["activity", "activity"])
        full, pointer = items
        self.assertEqual(full["summary"], "task reflection")
        self.assertEqual(full["fields"], {"task_id": "t", "task_type": "build",
                                          "rounds": 3, "cost_usd": 0.5,
                                          "error_count": 1, "goal": "ship it"})
        self.assertEqual(pointer["summary"], "project reflection pointer")
        self.assertEqual(pointer["fields"],
                         {"project_id": "alpha",
                          "reflection_path":
                              "projects/alpha/logs/task_reflections.jsonl"})
        # The pointer's recorded path is data, never opened by this reader.
        self.assertNotIn("project reflection body", json.dumps(items))
        project = self.reader.history("project:alpha:reflections")["data"]["items"]
        self.assertEqual([i["summary"] for i in project], ["task reflection"])
        self.assertEqual(project[0]["fields"]["task_id"], "t2")

    def test_dialogue_block_types_and_truncation(self):
        result = self.reader.dialogue(limit=50)
        blocks = result["data"]["blocks"]
        self.assertEqual([b["type"] for b in blocks],
                         ["summary", "era", "gap", "unknown", "unknown", "summary"])
        self.assertEqual(len({b["block_id"] for b in blocks}), len(blocks))
        self.assertEqual(blocks[0]["range"], [0, 100])
        self.assertEqual(blocks[0]["message_count"], 100)
        self.assertIsNone(blocks[0]["gap_id"])
        self.assertEqual(blocks[2]["gap_id"], "g-1")
        self.assertFalse(blocks[0]["truncated"])
        self.assertEqual(blocks[0]["content_bytes"], len(b"summary one"))
        long_block = blocks[-1]
        self.assertTrue(long_block["truncated"])
        self.assertEqual(long_block["content_bytes"], 8192)
        self.assertEqual(len(long_block["content"].encode("utf-8")), 8192)
        # A record with a non-string content and a non-object entry are gaps,
        # never invented blocks.
        self.assertEqual(sum(g["count"] for g in result["gaps"]
                             if g["scope"] == "dialogue_blocks.json"
                             and g["reason"] == "malformed_record"), 2)
        # The host consolidator never writes `type: "gap"`. It records a durable
        # discontinuity as `type: "summary"` carrying a `gap_id` and a
        # "[MEMORY GAP]" content marker, and its own predicates are
        # `bool(block["gap_id"])` and the content marker. Classifying on the
        # type field alone would label every real discontinuity a summary,
        # which asserts the opposite of what the block records.
        self.write("memory/dialogue_blocks.json", json.dumps([
            {"ts": "1", "type": "summary", "gap_id": "gap:x",
             "content": "[MEMORY GAP] the cursor generation was not located."},
            {"ts": "2", "type": "summary",
             "content": "[MEMORY GAP] no gap_id was written for this one."},
            {"ts": "3", "type": "summary", "gap_id": "",
             "content": "an ordinary consolidated summary"},
            {"ts": "4", "type": "era", "gap_id": None,
             "content": "older summaries folded together"}]))
        resolved = self.reader.dialogue(limit=50)["data"]["blocks"]
        self.assertEqual([b["type"] for b in resolved],
                         ["gap", "gap", "summary", "era"])
        self.assertEqual(resolved[0]["gap_id"], "gap:x")
        self.assertIsNone(resolved[1]["gap_id"])

    def test_dialogue_truncation_stops_on_a_character_boundary(self):
        self.write("memory/dialogue_blocks.json", json.dumps(
            [{"ts": "1", "type": "summary", "content": "é" * 5000}]))
        block = self.reader.dialogue()["data"]["blocks"][0]
        self.assertTrue(block["truncated"])
        self.assertLessEqual(block["content_bytes"], 8192)
        self.assertEqual(block["content"], "é" * (block["content_bytes"] // 2))

    def test_lone_surrogates_are_rejected_as_malformed_records(self):
        self.write("memory/dialogue_blocks.json",
                   '[{"ts":"1","type":"summary","content":"\\ud800"}]')
        dialogue = self.reader.dialogue()
        self.assertEqual(dialogue["data"]["blocks"], [])
        self.assertTrue(any(g["scope"] == "dialogue_blocks.json"
                            and g["reason"] == "malformed_record"
                            for g in dialogue["gaps"]))

        self.write("memory/identity_journal.jsonl",
                   '{"ts":"1","old_content":"\\ud800","new_content":"ok"}\n')
        history = self.reader.history("identity")
        self.assertEqual(history["data"]["items"], [])
        self.assertTrue(any(g["scope"] == "identity_journal.jsonl"
                            and g["reason"] == "malformed_record"
                            for g in history["gaps"]))

        for field in ("last_consolidated_offset", "chat_log_signature",
                      "last_consolidated_at"):
            self.write("memory/dialogue_meta.json",
                       '{"%s":"\\ud800"}' % field)
            meta = self.reader.dialogue()["data"]["meta"]
            self.assertFalse(meta["available"], field)
            self.assertEqual(meta["reason"], "malformed_json", field)
            json.dumps(meta, ensure_ascii=False).encode("utf-8")
        safe = self.reader._json_safe({"nested": ["\ud800"]})
        self.assertTrue(self.reader._unicode_scalar_safe(safe))
        json.dumps(safe, ensure_ascii=False).encode("utf-8")

    def test_dialogue_meta_present_missing_and_malformed(self):
        meta = self.reader.dialogue()["data"]["meta"]
        self.assertTrue(meta["available"])
        self.assertIsNone(meta["reason"])
        self.assertEqual(meta["last_consolidated_offset"], 600)
        self.assertEqual(meta["last_consolidated_at"], "2026-09-01")
        self.assertEqual(meta["chat_log_signature"]["generation"]["first_line_sha256"],
                         "abc")
        self.write("memory/dialogue_meta.json", "{not json")
        broken = self.reader.dialogue()["data"]["meta"]
        self.assertFalse(broken["available"])
        self.assertEqual(broken["reason"], "malformed_json")
        self.assertIsNone(broken["last_consolidated_offset"])
        self.write("memory/dialogue_meta.json", json.dumps(["a list"]))
        self.assertEqual(self.reader.dialogue()["data"]["meta"]["reason"],
                         "malformed_json")
        (self.root / "memory/dialogue_meta.json").unlink()
        absent = self.reader.dialogue()["data"]["meta"]
        self.assertFalse(absent["available"])
        self.assertEqual(absent["reason"], "missing")
        self.assertIsNone(absent["chat_log_signature"])

    def test_dialogue_pages_and_rejects_stale_revisions(self):
        first = self.reader.dialogue(limit=2)["data"]
        self.assertEqual(len(first["blocks"]), 2)
        collected = list(first["blocks"])
        cursor, revision = first["next_cursor"], first["revision"]
        while cursor:
            page = self.reader.dialogue(cursor=cursor, limit=2,
                                        revision=revision)["data"]
            collected.extend(page["blocks"])
            cursor = page["next_cursor"]
        self.assertEqual([b["block_id"] for b in collected],
                         [b["block_id"] for b in
                          self.reader.dialogue(limit=50)["data"]["blocks"]])
        with self.assertRaises(AtlasError) as caught:
            self.reader.dialogue(revision="stale")
        self.assertEqual(caught.exception.code, "revision_drift")
        # The revision binds dialogue_meta.json as well as the blocks file.
        stale = self.reader.dialogue()["data"]["revision"]
        self.write("memory/dialogue_meta.json", json.dumps({"last_consolidated_offset": 7}))
        with self.assertRaises(AtlasError) as caught:
            self.reader.dialogue(revision=stale)
        self.assertEqual(caught.exception.code, "revision_drift")

    def test_dialogue_missing_or_malformed_file_is_honest(self):
        self.write("memory/dialogue_blocks.json", json.dumps({"not": "a list"}))
        result = self.reader.dialogue()
        self.assertEqual(result["data"]["blocks"], [])
        self.assertTrue(any(g["reason"] == "malformed_array"
                            for g in result["gaps"]))
        self.write("memory/dialogue_blocks.json", "{{{")
        result = self.reader.dialogue()
        self.assertEqual(result["data"]["blocks"], [])
        self.assertTrue(any(g["reason"] == "malformed_json" for g in result["gaps"]))
        (self.root / "memory/dialogue_blocks.json").unlink()
        with self.assertRaises(AtlasError) as caught:
            self.reader.dialogue()
        self.assertEqual(caught.exception.code, "source_not_found")

    def test_empty_and_non_utf8_new_sources_stay_honest(self):
        self.write("memory/registry.md", "")
        empty = self.reader.document("registry")["data"]
        self.assertEqual(empty["content"], "")
        self.assertTrue(empty["complete"])
        (self.root / "memory/deep_review.md").write_bytes(b"bad\xffbytes")
        item = next(i for i in self.reader.catalog(limit=200)["data"]["items"]
                    if i["id"] == "deep_review")
        self.assertIsNone(item["revision"])
        self.assertEqual(item["read_error"], "invalid_utf8")
        (self.root / "logs/task_reflections.jsonl").write_bytes(b"y" * (MAX_FILE + 1))
        result = self.reader.history("reflections")
        self.assertEqual(result["data"]["items"], [])
        self.assertTrue(any(g["reason"] == "file_too_large" for g in result["gaps"]))

    def test_catalog_revision_pages_document_and_catalog(self):
        first = self.reader.catalog(limit=2)
        items = list(first["data"]["items"])
        cursor = first["data"]["next_cursor"]
        while cursor:
            page = self.reader.catalog(cursor=cursor, limit=2)
            items.extend(page["data"]["items"])
            cursor = page["data"]["next_cursor"]
        self.assertEqual([item["id"] for item in items],
                         [item["id"] for item in self.reader.catalog()["data"]["items"]])
        identity = next(item for item in items if item["id"] == "identity")
        data = self.reader.document("identity", revision=identity["revision"], limit=3)["data"]
        self.assertEqual(data["revision"], identity["revision"])
        chunks = [data["content"]]
        while not data["complete"]:
            data = self.reader.document("identity", cursor=data["next_cursor"],
                                        revision=identity["revision"], limit=3)["data"]
            chunks.append(data["content"])
        self.assertEqual("".join(chunks), "Straße <script> café")

    def test_unreadable_source_keeps_catalog_entry_with_null_revision(self):
        oversize = self.root / "memory/knowledge/patterns.md"
        oversize.write_bytes(b"x" * (MAX_FILE + 1))
        result = self.reader.catalog()
        item = next(i for i in result["data"]["items"]
                    if i["id"] == "knowledge:patterns")
        # Listed, honest about the failure, and not given a substitute digest.
        self.assertIsNone(item["revision"])
        self.assertEqual(item["read_error"], "file_too_large")
        self.assertEqual(item["bytes"], MAX_FILE + 1)
        self.assertTrue(any(g["scope"] == "knowledge:patterns"
                            and g["reason"] == "file_too_large"
                            for g in result["gaps"]))
        healthy = next(i for i in result["data"]["items"] if i["id"] == "identity")
        self.assertIsNone(healthy["read_error"])
        self.assertEqual(healthy["revision"],
                         self.reader.document("identity")["data"]["revision"])
        # A stat-derived hash must never appear where a content digest belongs.
        self.assertNotEqual(item["revision"],
                            self.reader._revision((("knowledge:patterns", oversize),)))

    def test_document_utf8_pages_advance_and_reject_nonboundary(self):
        data = self.reader.document("identity", limit=1)["data"]
        chunks = [data["content"]]
        while not data["complete"]:
            old = data["offset"]
            data = self.reader.document("identity", cursor=data["next_cursor"],
                                        revision=data["revision"], limit=1)["data"]
            self.assertGreater(data["offset"], old)
            chunks.append(data["content"])
        self.assertEqual("".join(chunks), "Straße <script> café")
        revision = self.reader.document("identity")["data"]["revision"]
        bad_offset = "Straße <script> café".encode("utf-8").index(b"\xc3") + 1
        bad = self.reader._make_cursor("document", revision, "identity", bad_offset)
        with self.assertRaises(AtlasError):
            self.reader.document("identity", cursor=bad, revision=revision, limit=2)

    def test_history_authentic_schemas_and_topic_filter(self):
        identity = self.reader.history("identity")
        reps = [x["representation"] for x in identity["data"]["items"]]
        self.assertEqual(reps.count("snapshot"), 2)
        self.assertEqual(reps.count("digest_preview"), 1)
        digest = next(x for x in identity["data"]["items"]
                      if x["representation"] == "digest_preview")
        self.assertEqual(digest["fields"]["old_preview"], "before")
        snap = next(x for x in identity["data"]["items"]
                    if x.get("fields", {}).get("snapshot_side") == "old")
        self.assertEqual(self.reader.history_event("identity", snap["event_id"])["data"]["content"],
                         "OLD café")
        facts = self.reader.history("knowledge:facts")
        self.assertFalse(any(g["reason"] == "unattributed_record" for g in facts["gaps"]))
        contents = [self.reader.history_event("knowledge:facts", e["event_id"])["data"]["content"]
                    for e in facts["data"]["items"] if e["representation"] == "snapshot"]
        self.assertEqual(contents, ["old facts", "new facts"])
        digest = next(x for x in facts["data"]["items"]
                      if x["representation"] == "digest_preview")
        self.assertEqual(digest["fields"]["digest_preview"], "facts digest")
        self.assertEqual(digest["fields"]["old_preview"], "old p")
        self.assertEqual(digest["fields"]["new_preview"], "new p")
        self.assertEqual(digest["fields"]["old_sha256"], "2")
        with self.assertRaises(AtlasError) as caught:
            self.reader.history_event("knowledge:facts", digest["event_id"])
        self.assertEqual(caught.exception.code, "no_snapshot")

    def test_scratchpad_block_truth(self):
        response = self.reader.history("scratchpad")
        items = response["data"]["items"]
        failed = next(x for x in items if x["kind"] == "block_append_failed")
        self.assertEqual(failed["representation"], "activity")
        snapshots = [x for x in items if x["representation"] == "snapshot"]
        values = [self.reader.history_event("scratchpad", x["event_id"])["data"]["content"]
                  for x in snapshots]
        self.assertEqual(values, ["complete block", "appended complete", "retired complete"])
        self.assertTrue(any(g["scope"] == "scratchpad_blocks.json" and
                            g["reason"] == "malformed_record" for g in response["gaps"]))
        # Every scratchpad snapshot is labelled as one block, never as a
        # version of the whole scratchpad document.
        for event in snapshots:
            self.assertEqual(event["fields"]["snapshot_scope"], "block")

    def test_scratchpad_journal_reads_the_native_writer_shapes(self):
        response = self.reader.history("scratchpad")
        items = response["data"]["items"]
        by_kind = {x["kind"]: x for x in items if x["kind"].startswith(("block_", "scratchpad_"))}
        # The discriminator is "type"; the flat eviction record carries the
        # retired block content outside any nested "block" object.
        evicted = by_kind["block_evicted"]
        self.assertEqual(evicted["representation"], "snapshot")
        self.assertEqual(evicted["fields"]["evicted_block_source"], "user")
        self.assertEqual(evicted["fields"]["evicted_block_ts"], "3")
        self.assertEqual(evicted["fields"]["source_ref"], {"kind": "task", "id": "t-1"})
        self.assertEqual(
            self.reader.history_event("scratchpad", evicted["event_id"])["data"]["content"],
            "retired complete")
        appended = by_kind["block_appended"]
        self.assertEqual(appended["representation"], "snapshot")
        self.assertEqual(appended["fields"]["source"], "user")
        self.assertEqual(appended["fields"]["metadata"], {"tag": "note"})
        # A failed append stored nothing, so its block is never a snapshot.
        failed = by_kind["block_append_failed"]
        self.assertEqual(failed["representation"], "activity")
        with self.assertRaises(AtlasError) as caught:
            self.reader.history_event("scratchpad", failed["event_id"])
        self.assertEqual(caught.exception.code, "no_snapshot")
        # An unknown but well-formed record type is activity, not malformed.
        unknown = by_kind["scratchpad_trimmed"]
        self.assertEqual(unknown["representation"], "activity")
        self.assertFalse(any(g["scope"] == "scratchpad_journal.jsonl" and
                             g["reason"] == "malformed_record" for g in response["gaps"]))
        bodies = [self.reader.history_event("scratchpad", x["event_id"])["data"]["content"]
                  for x in items if x["representation"] == "snapshot"]
        self.assertNotIn("never stored", "\n".join(bodies))
        # No event claims to be the whole scratchpad document.
        current = self.reader.document("scratchpad")["data"]["content"]
        self.assertNotIn(current, bodies)

    def test_history_event_utf8_paging(self):
        event = next(x for x in self.reader.history("identity")["data"]["items"]
                     if x.get("fields", {}).get("snapshot_side") == "new")
        page = self.reader.history_event("identity", event["event_id"], limit=1)["data"]
        chunks = [page["content"]]
        while not page["complete"]:
            page = self.reader.history_event("identity", event["event_id"],
                cursor=page["next_cursor"], revision=page["revision"], limit=1)["data"]
            chunks.append(page["content"])
        self.assertEqual("".join(chunks), "NEW Straße")

    def test_content_digested_is_honoured_only_as_a_literal_true(self):
        # "false" and 1 are not the writer's digested flag. Reading them as
        # true would hide retained content behind an invented preview.
        self.write("memory/identity_journal.jsonl", "".join((
            self.jline({"ts": "1", "content_digested": "false",
                        "old_content": "identity old", "new_content": "identity new"}),
            self.jline({"ts": "2", "content_digested": 1,
                        "digest_preview": "not a flag", "new_content": "identity kept"}),
            self.jline({"ts": "3", "content_digested": True,
                        "digest_preview": "identity digest"}))))
        self.write("memory/knowledge_history.jsonl", "".join((
            self.jline({"ts": "1", "topic": "facts", "content_digested": "false",
                        "old_content": "kb old", "new_content": "kb new"}),
            self.jline({"ts": "2", "topic": "facts", "content_digested": True,
                        "digest_preview": "kb digest"}))))
        for source, expected in (("identity", ["identity old", "identity new",
                                               "identity kept"]),
                                 ("knowledge:facts", ["kb old", "kb new"])):
            items = self.reader.history(source)["data"]["items"]
            digests = [x for x in items if x["representation"] == "digest_preview"]
            self.assertEqual(len(digests), 1, source)
            contents = [self.reader.history_event(source, x["event_id"])["data"]["content"]
                        for x in items if x["representation"] == "snapshot"]
            self.assertEqual(contents, expected)

    def test_invalid_utf8_source_is_listed_without_a_revision(self):
        target = self.root / "memory/knowledge/patterns.md"
        target.write_bytes(b"good\xffbad")
        result = self.reader.catalog()
        item = next(i for i in result["data"]["items"] if i["id"] == "knowledge:patterns")
        # No digest is published for bytes no page can return as text.
        self.assertIsNone(item["revision"])
        self.assertEqual(item["read_error"], "invalid_utf8")
        self.assertEqual(item["bytes"], target.lstat().st_size)
        self.assertTrue(any(g["scope"] == "knowledge:patterns" and
                            g["reason"] == "invalid_utf8" for g in result["gaps"]))
        with self.assertRaises(AtlasError) as caught:
            self.reader.document("knowledge:patterns")
        self.assertEqual(caught.exception.code, "invalid_utf8")

    def test_graph_reports_a_store_that_vanished_after_discovery(self):
        # The store is listed by discovery and gone by the time the task_id
        # scan stats it; that race is a gap, not a crash.
        ghost = Source("knowledge:ghost", "knowledge", "ghost",
                       self.root / "memory/knowledge/patterns.md", "activity",
                       (self.root / "memory/knowledge/ghost.jsonl",))
        discovered = self.reader._sources

        def with_ghost():
            sources, gaps = discovered()
            return sorted(sources + [ghost], key=lambda s: s.id), gaps

        self.reader._sources = with_ghost
        try:
            result = self.reader.graph("scratchpad")
        finally:
            self.reader._sources = discovered
        self.assertTrue(any(g["scope"] == "ghost.jsonl" and g["reason"] == "unreadable"
                            for g in result["gaps"]))

    def test_search_spans_whole_characters_that_case_folding_expands(self):
        # "Straße" folds to "strasse": a match that stops inside that expansion
        # still covers the whole original character, and the expansion is not
        # reported twice.
        hits = [h for h in self.reader.search("s")["data"]["items"]
                if h["id"] == "identity"]
        line = "Straße <script> café"
        self.assertEqual([h["excerpt"][h["match_start"]:h["match_end"]] for h in hits],
                         ["S", "ß", "s"])
        self.assertEqual([h["column"] for h in hits], [1, 5, 9])
        self.assertEqual(len({(h["line"], h["column"]) for h in hits}), len(hits))
        for query in ("ß", "ss"):
            folded = [h for h in self.reader.search(query)["data"]["items"]
                      if h["id"] == "identity"]
            self.assertEqual(len(folded), 1, query)
            hit = folded[0]
            self.assertEqual(hit["excerpt"][hit["match_start"]:hit["match_end"]], "ß")
            self.assertEqual(line[hit["column"] - 1], "ß")

    def test_search_casefold_maps_original_offsets_and_xss_is_data(self):
        hit = self.reader.search("STRASSE")["data"]["items"][0]
        self.assertEqual(hit["excerpt"][hit["match_start"]:hit["match_end"]], "Straße")
        self.assertIn("<script>", self.reader.search("<script>")["data"]["items"][0]["excerpt"])

    ALLOWED_KINDS = {"markdown_link", "wiki_link", "journal_source_ref",
                     "shared_task_id"}

    def test_graph_link_kinds_are_distinct_and_bare_mentions_are_not_edges(self):
        graph = self.reader.graph("knowledge:facts")["data"]
        edges = graph["edges"]
        self.assertTrue(edges)
        self.assertTrue(self.ALLOWED_KINDS.issuperset({e["kind"] for e in edges}))
        markdown = {e["target"] for e in edges if e["kind"] == "markdown_link"}
        wiki = {e["target"] for e in edges if e["kind"] == "wiki_link"}
        # Markdown and wiki links are separate authoring acts and separate kinds.
        self.assertEqual(markdown, {"knowledge:patterns",
                                    "project:alpha:knowledge:notes", "world"})
        self.assertEqual(wiki, {"knowledge:patterns"})
        for edge in edges:
            self.assertIn("basis", edge)
            self.assertIsInstance(edge["evidence"]["source_excerpt"], str)
        self.assertEqual(
            {e["basis"] for e in edges if e["kind"] == "markdown_link"},
            {"Markdown link in this document"})
        self.assertEqual({e["basis"] for e in edges if e["kind"] == "wiki_link"},
                         {"Wiki link in this document"})
        # The document names "identity", "scratchpad" and "knowledge:patterns"
        # in prose, and links to an absolute path and an http URL. None of those
        # is a provable link.
        node_ids = {n["id"] for n in graph["nodes"]}
        self.assertNotIn("identity", node_ids)
        self.assertNotIn("scratchpad", node_ids)

    def wiki_edges(self, body, focus="knowledge:facts",
                   rel="memory/knowledge/facts.md"):
        """Rewrite the focus document to ``body`` and return its wiki edges."""
        self.write(rel, body)
        edges = self.reader.graph(focus)["data"]["edges"]
        return [e for e in edges if e["kind"] == "wiki_link"]

    def test_graph_wiki_link_accepts_an_exact_catalog_source_id(self):
        # The widget's Markdown resolver renders [[knowledge:patterns]] as a
        # link, so the graph must agree the relationship exists.
        wiki = self.wiki_edges("facts [[knowledge:patterns]]")
        self.assertEqual(len(wiki), 1)
        edge = wiki[0]
        self.assertEqual(edge["target"], "knowledge:patterns")
        self.assertEqual(edge["kind"], "wiki_link")
        self.assertEqual(edge["basis"], "Wiki link in this document")
        self.assertIn("[[knowledge:patterns]]", edge["evidence"]["source_excerpt"])
        # Repeating the same authored link is still one edge (dedup by
        # (target, kind, basis)), and it never becomes a markdown_link.
        again = self.wiki_edges("[[knowledge:patterns]] and [[knowledge:patterns]]")
        self.assertEqual(len(again), 1)
        # A [[…]] of the focus's own id is not a self-edge.
        self.assertEqual(self.wiki_edges("[[knowledge:facts]]"), [])

    def test_graph_wiki_link_path_forms_are_unchanged(self):
        self.write("memory/knowledge/tooling.md", "tooling body")
        # Sibling forms, resolved relative to the focus document's directory.
        for body in ("[[tooling]]", "[[tooling.md]]",
                     "[[../knowledge/tooling.md]]"):
            wiki = self.wiki_edges(body)
            self.assertEqual([e["target"] for e in wiki], ["knowledge:tooling"], body)
            self.assertEqual([e["basis"] for e in wiki],
                             ["Wiki link in this document"], body)
        # A directory-qualified path form from a focus one level up.
        wiki = self.wiki_edges("[[knowledge/tooling.md]]", focus="identity",
                               rel="memory/identity.md")
        self.assertEqual([e["target"] for e in wiki], ["knowledge:tooling"])

    def md_edges(self, body, focus="knowledge:facts",
                 rel="memory/knowledge/facts.md"):
        """Rewrite the focus document to ``body`` and return its Markdown edges."""
        self.write(rel, body)
        edges = self.reader.graph(focus)["data"]["edges"]
        return [e for e in edges if e["kind"] == "markdown_link"]

    def test_graph_markdown_link_accepts_an_exact_catalog_source_id(self):
        # The reader renders [tooling](knowledge:tooling) as a working
        # cross-reference, so the graph has to agree the link exists. Before
        # this, the id form was honoured for wiki links only and the two tabs
        # disagreed about the same document.
        self.write("memory/knowledge/tooling.md", "tooling body")
        edges = self.md_edges("facts [tooling notes](knowledge:tooling)")
        self.assertEqual([e["target"] for e in edges], ["knowledge:tooling"])
        self.assertEqual(edges[0]["basis"], "Markdown link in this document")
        self.assertIn("knowledge:tooling", edges[0]["evidence"]["source_excerpt"])
        # Still whole-string equality, and still not a self-edge.
        self.assertEqual(self.md_edges("[x](knowledge:tool)"), [])
        self.assertEqual(self.md_edges("[x](knowledge:facts)"), [])
        # Link-shaped text that the reader presents as an image, code, or an
        # escaped construct is not an authored cross-reference.
        ignored = (
            "![diagram](tooling.md)",
            "`[example](tooling.md)`",
            "```md\n[example](tooling.md)\n```",
            "    [example](tooling.md)",
            r"\[escaped](tooling.md)",
        )
        for body in ignored:
            self.assertEqual(self.md_edges(body), [], body)
        self.assertEqual(
            [edge["target"] for edge in self.md_edges("[real](tooling.md)")],
            ["knowledge:tooling"])

    def test_graph_links_resolve_to_non_markdown_catalog_sources(self):
        # The contract defines a link edge as any target that lexically
        # resolves to an allowlisted catalogue source, and the catalogue holds
        # JSON and NDJSON sources. A ".md"-only gate made those edges
        # impossible to author while the docs promised them.
        cases = {"../dialogue_blocks.json": "dialogue",
                 "../../logs/task_reflections.jsonl": "reflections",
                 "../../projects/alpha/journal.jsonl": "project:alpha:journal"}
        for href, target in cases.items():
            edges = self.md_edges("chronicle [pointer](%s)" % href)
            self.assertEqual([e["target"] for e in edges], [target], href)
            self.assertEqual(edges[0]["basis"], "Markdown link in this document")
        # The same targets through the wiki form, which shares the resolver.
        wiki = self.wiki_edges("[[../dialogue_blocks.json]]")
        self.assertEqual([e["target"] for e in wiki], ["dialogue"])
        # A non-Markdown path that is not a catalogue source is still nothing.
        self.assertEqual(self.md_edges("[meta](../dialogue_meta.json)"), [])
        self.assertEqual(self.md_edges("[chat](../../logs/chat.jsonl)"), [])

    def test_graph_reads_a_focus_that_is_its_own_history_store_once(self):
        # For reflections, project reflections and project journals the history
        # store IS the focus file. Reading it for the link scan and again for
        # the record scan charged the same bytes twice against the scan budget,
        # contradicting "each graph store is read at most once" and producing a
        # premature scan_limit that omitted provable edges.
        #
        # Counted at the filesystem, not at the cache in front of it: the
        # revision now digests the same bytes the scan reads, so what has to
        # stay true is that the whole bound phase opens the file once. The
        # second open belongs to the closing drift check, which runs outside
        # the phase precisely so that it re-reads for real.
        reads = []
        original = self.reader._read_uncached

        def counting(path):
            reads.append(str(path))
            return original(path)

        self.reader._read_uncached = counting
        try:
            result = self.reader.graph("reflections")
        finally:
            self.reader._read_uncached = original
        focus = str(self.reader.root / "logs/task_reflections.jsonl")
        self.assertEqual(reads.count(focus), 2, reads)
        # No file anywhere in the request is opened a third time.
        self.assertEqual(max(reads.count(path) for path in set(reads)), 2, reads)
        self.assertFalse(any(g["scope"] == "graph" and g["reason"] == "scan_limit"
                             for g in result["gaps"]))
        # The records were still available: the shared task_id edges are there.
        self.assertTrue([e for e in result["data"]["edges"]
                         if e["kind"] == "shared_task_id"])

    def test_non_finite_numbers_are_malformed_records_not_a_broken_response(self):
        # json.loads accepts the NaN/Infinity extensions; the response
        # serializer writes with allow_nan=False, so letting one through turned
        # a valid request into a generic 500.
        self.write("memory/dialogue_blocks.json",
                   '[{"ts":"1","type":"summary","content":"ok","message_count":NaN},'
                   ' {"ts":"2","type":"summary","content":"kept"}]')
        dialogue = self.reader.dialogue()
        self.assertEqual([b["content"] for b in dialogue["data"]["blocks"]], ["kept"])
        self.assertTrue(any(g["scope"] == "dialogue_blocks.json"
                            and g["reason"] == "malformed_record"
                            for g in dialogue["gaps"]))
        json.dumps(dialogue, allow_nan=False)

        self.write("memory/identity_journal.jsonl",
                   '{"ts":"1","old_content":"a","new_content":"b","rounds":Infinity}\n'
                   '{"ts":"2","old_content":"b","new_content":"c"}\n')
        history = self.reader.history("identity")
        self.assertTrue(any(g["scope"] == "identity_journal.jsonl"
                            and g["reason"] == "malformed_record"
                            for g in history["gaps"]))
        json.dumps(history, allow_nan=False)

    # Deep enough that a recursive walk of the parsed value exhausts the
    # interpreter stack, and small enough to sit far below MAX_FILE and, for
    # the object form, below MAX_LINE.
    DEEP_ARRAY = "[" * 20_000 + "]" * 20_000
    DEEP_OBJECT = '{"a":' * 6_000 + "1" + "}" * 6_000

    def assertParseGap(self, gaps, scope):
        """A deep value is an observation about the file, never a 500.

        Which gap it is depends on where the depth is refused: a decoder that
        gives up parsing reports ``malformed_json``, and one that parses the
        value hands a record this reader will not walk past MAX_JSON_DEPTH,
        which is ``malformed_record``. Both are documented; a RecursionError
        escaping to plugin.py as a generic ``internal_error`` is not.
        """
        self.assertTrue(
            any(g["scope"] == scope
                and g["reason"] in ("malformed_json", "malformed_record")
                for g in gaps), gaps)

    def test_deeply_nested_dialogue_records_are_a_gap_not_a_crash(self):
        # WHERE the depth is refused is an interpreter fact, not a contract:
        # a decoder that gives up on the nesting costs the whole ONE-array
        # chronicle (no sibling is recoverable from an array that never
        # parsed), while a decoder that parses it hands this reader a record
        # it refuses past MAX_JSON_DEPTH and only that record is lost. Both
        # are honest; what must hold on every interpreter is that the refusal
        # is a disclosed gap, nothing is fabricated, and nothing crashes.
        for nested in (self.DEEP_ARRAY, self.DEEP_OBJECT):
            self.write("memory/dialogue_blocks.json",
                       "[" + nested + ',{"ts":"2","type":"summary","content":"kept"}]')
            dialogue = self.reader.dialogue()
            self.assertIn([b["content"] for b in dialogue["data"]["blocks"]],
                          ([], ["kept"]))
            self.assertParseGap(dialogue["gaps"], "dialogue_blocks.json")
            json.dumps(dialogue, allow_nan=False)
        # The provenance file is read through the same guard, and a chronicle
        # whose meta cannot be parsed still serves its blocks.
        self.write("memory/dialogue_blocks.json",
                   '[{"ts":"1","type":"summary","content":"kept"}]')
        self.write("memory/dialogue_meta.json", self.DEEP_ARRAY)
        dialogue = self.reader.dialogue()
        self.assertEqual(dialogue["data"]["meta"]["available"], False)
        self.assertEqual(dialogue["data"]["meta"]["reason"], "malformed_json")
        self.assertEqual([b["content"] for b in dialogue["data"]["blocks"]], ["kept"])

    def test_deeply_nested_history_records_are_a_gap_not_a_crash(self):
        for nested in (self.DEEP_ARRAY, self.DEEP_OBJECT):
            self.write("memory/identity_journal.jsonl",
                       '{"ts":"1","old_content":"a","new_content":"b",'
                       '"rounds":' + nested + "}\n"
                       '{"ts":"2","old_content":"b","new_content":"c"}\n')
            history = self.reader.history("identity")
            self.assertParseGap(history["gaps"], "identity_journal.jsonl")
            self.assertTrue(history["data"]["items"])
            json.dumps(history, allow_nan=False)
        # The array store the scratchpad keeps is parsed by the other helper.
        self.write("memory/scratchpad_blocks.json", "[" + self.DEEP_OBJECT + "]")
        scratchpad = self.reader.history("scratchpad")
        self.assertParseGap(scratchpad["gaps"], "scratchpad_blocks.json")
        json.dumps(scratchpad, allow_nan=False)

    def test_graph_wiki_link_ignores_unknown_targets_and_bare_prose(self):
        for body in ("[[not-a-real-source]]",
                     "knowledge:patterns is discussed here",
                     "see knowledge/patterns.md for more",
                     "[[knowledge:patterns-extra]]",
                     "[[knowledge:pat]]", "[[patterns:]]", "[[:patterns]]"):
            self.assertEqual(self.wiki_edges(body), [], body)

    def test_graph_wiki_link_id_form_cannot_escape_or_reach_a_denied_path(self):
        outside = self.root.parent / "outside.md"
        outside.write_text("outside the root", encoding="utf-8")
        try:
            for body in ("[[../../etc/passwd]]", "[[/etc/passwd]]",
                         "[[../../outside]]", "[[../../outside.md]]",
                         "[[..\\..\\outside.md]]", "[[file:///etc/passwd]]",
                         "[[https://example.com/x.md]]",
                         # Derived indexes are not catalog sources, by id or by path.
                         "[[index-full]]", "[[knowledge:index-full]]",
                         "[[knowledge/index-full.md]]"):
                self.assertEqual(self.wiki_edges(body), [], body)
        finally:
            outside.unlink()

    def test_graph_journal_source_ref_only_resolves_inside_the_root(self):
        edges = self.reader.graph("scratchpad")["data"]["edges"]
        refs = [e for e in edges if e["kind"] == "journal_source_ref"]
        self.assertEqual([e["target"] for e in refs], ["knowledge:patterns"])
        self.assertEqual(refs[0]["basis"], "Journal read reference")
        excerpt = refs[0]["evidence"]["source_excerpt"]
        self.assertIn("memory/knowledge/patterns.md", excerpt)
        self.assertIn("e-1", excerpt)
        # The recorded "../../etc/passwd" read produced no edge at all.
        self.assertNotIn("passwd", " ".join(
            e["evidence"]["source_excerpt"] for e in edges))

    def test_graph_shared_task_id_names_the_shared_identifier(self):
        edges = self.reader.graph("scratchpad")["data"]["edges"]
        shared = [e for e in edges if e["kind"] == "shared_task_id"]
        self.assertTrue(shared)
        self.assertEqual({e["basis"] for e in shared}, {"shared task_id t"})
        self.assertIn("project:alpha:journal", {e["target"] for e in shared})
        self.assertIn("reflections", {e["target"] for e in shared})

    def test_graph_only_emits_the_four_provable_kinds(self):
        for focus in [item["id"] for item in
                      self.reader.catalog(limit=200)["data"]["items"]]:
            edges = self.reader.graph(focus)["data"]["edges"]
            self.assertTrue(self.ALLOWED_KINDS.issuperset({e["kind"] for e in edges}),
                            focus)

    def test_graph_without_links_is_honestly_empty(self):
        graph = self.reader.graph("world")["data"]
        self.assertEqual(graph["edges"], [])
        # Only the focus itself: no neighbour is manufactured to fill the view.
        self.assertEqual([n["id"] for n in graph["nodes"]], ["world"])

    def test_graph_reports_a_truncated_store_scan(self):
        saved = memory_reader.MAX_GRAPH_FILES
        memory_reader.MAX_GRAPH_FILES = 0
        try:
            result = self.reader.graph("scratchpad")
        finally:
            memory_reader.MAX_GRAPH_FILES = saved
        self.assertTrue(any(g["scope"] == "graph" and g["reason"] == "scan_limit"
                            for g in result["gaps"]))
        self.assertEqual(result["data"]["edges"], [])

    def test_graph_edge_bound_reports_exact_omitted_count(self):
        links, refs, rows = [], [], []
        for index in range(99):
            slug = f"edge-{index:02d}"
            self.write(f"memory/knowledge/{slug}.md", slug)
            links.extend((f"[m](knowledge/{slug}.md)",
                          f"[[knowledge/{slug}.md]]"))
            task_id = f"task-{index:02d}"
            refs.append(self.jline({
                "ts": str(index), "type": "scratchpad_read", "task_id": task_id,
                "source_ref": {"read": {"arguments": {
                    "path": f"memory/knowledge/{slug}.md"}}}}))
            rows.append(self.jline({"ts": str(index), "topic": slug,
                                    "task_id": task_id, "kind": "updated"}))
        self.write("memory/scratchpad.md", " ".join(links))
        self.write("memory/scratchpad_journal.jsonl", "".join(refs))
        self.write("memory/knowledge_journal.jsonl", "".join(rows))

        result = self.reader.graph("scratchpad", limit=100)
        self.assertEqual(len(result["data"]["edges"]), MAX_GRAPH_EDGES)
        limit_gap = next(g for g in result["gaps"]
                         if g["scope"] == "graph" and g["reason"] == "edge_limit")
        self.assertEqual(limit_gap["count"], 96)

    def test_dialogue_limit_bounds(self):
        for bad in (0, 51, True, "10"):
            with self.assertRaises(AtlasError) as caught:
                self.reader.dialogue(limit=bad)
            self.assertEqual(caught.exception.code, "invalid_limit")
        self.assertEqual(len(self.reader.dialogue()["data"]["blocks"]), 6)

    def test_graph_revision_binds_the_history_stores_it_reads(self):
        # Graph edges come from journal read references and shared task_ids,
        # which live in the history stores — not in the source documents. A
        # revision covering only the documents let those stores change (or
        # vanish, or appear) while the digest stayed put, so a client could
        # pin a revision and still be served a different edge set.
        first = self.reader.graph("scratchpad")["data"]
        pinned = first["revision"]
        documents_only = self.reader._aggregate_revision(self.reader._sources()[0])
        # History-only change: only the journal is touched.
        with (self.root / "memory/scratchpad_journal.jsonl").open(
                "a", encoding="utf-8") as handle:
            handle.write(self.jline({"ts": "10", "type": "scratchpad_read",
                                     "task_id": "t", "source_ref": {"read": {
                                         "arguments": {
                                             "path": "memory/identity.md"}}}}))
        # The document digest is blind to it; the graph digest is not.
        self.assertEqual(documents_only,
                         self.reader._aggregate_revision(self.reader._sources()[0]))
        self.assertNotEqual(pinned, self.reader.graph("scratchpad")["data"]["revision"])
        with self.assertRaises(AtlasError) as caught:
            self.reader.graph("scratchpad", revision=pinned)
        self.assertEqual(caught.exception.code, "revision_drift")
        self.assertEqual(caught.exception.status, 409)

    def rewrite_in_place(self, rel, old, new):
        """Change a file's content while every stat field stays identical.

        Same length, same inode, and ``st_mtime_ns`` restored afterwards: the
        four fields a stat-based revision digested are byte-for-byte what they
        were, so only a revision that hashes content can notice this edit.
        """
        path = self.root / rel
        self.assertEqual(len(old), len(new), "the rewrite must not change size")
        before = path.lstat()
        raw = path.read_bytes()
        self.assertIn(old, raw)
        with path.open("r+b") as handle:
            handle.seek(raw.index(old))
            handle.write(new)
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.lstat()
        self.assertEqual(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertNotEqual(raw, path.read_bytes())

    def test_every_revision_follows_content_not_stat_metadata(self):
        # The revision digested each file's name and stat tuple, never its
        # bytes. An in-place edit of the same length with the mtime restored
        # left dev, inode, size and mtime untouched, so a changed edge set,
        # history, search corpus or dialogue block could be served under a
        # pinned revision with both the before and the after drift check
        # agreeing that nothing had moved.
        graph_pinned = self.reader.graph("knowledge:facts")["data"]["revision"]
        history_pinned = self.reader.history("knowledge:patterns")["data"]["revision"]
        search_pinned = self.reader.search("sharedterm")["data"]["revision"]
        catalog_cursor = self.reader.catalog(limit=1)["data"]["next_cursor"]
        self.assertIsNotNone(catalog_cursor)
        catalog_pinned = self.reader._aggregate_revision(self.reader._sources()[0])
        dialogue_pinned = self.reader.dialogue()["data"]["revision"]

        self.rewrite_in_place("memory/knowledge/patterns.md",
                              b"patterns sharedterm", b"PATTERNS sharedterM")
        self.rewrite_in_place("memory/dialogue_blocks.json",
                              b"summary one", b"summary ONE")

        self.assertNotEqual(
            graph_pinned, self.reader.graph("knowledge:facts")["data"]["revision"])
        self.assertNotEqual(
            history_pinned,
            self.reader.history("knowledge:patterns")["data"]["revision"])
        self.assertNotEqual(
            search_pinned, self.reader.search("sharedterm")["data"]["revision"])
        self.assertNotEqual(
            catalog_pinned,
            self.reader._aggregate_revision(self.reader._sources()[0]))
        self.assertNotEqual(
            dialogue_pinned, self.reader.dialogue()["data"]["revision"])

        # And every route that accepts a pinned revision now rejects the old
        # one, with the same drift vocabulary as any other corpus change.
        for call in (lambda: self.reader.graph("knowledge:facts",
                                               revision=graph_pinned),
                     lambda: self.reader.history("knowledge:patterns",
                                                 revision=history_pinned),
                     lambda: self.reader.search("sharedterm",
                                                revision=search_pinned),
                     lambda: self.reader.catalog(cursor=catalog_cursor, limit=1),
                     lambda: self.reader.dialogue(revision=dialogue_pinned)):
            with self.assertRaises(AtlasError) as caught:
                call()
            self.assertEqual(caught.exception.code, "revision_drift")
            self.assertEqual(caught.exception.status, 409)

    def test_revision_separates_a_missing_file_from_an_empty_one(self):
        store = self.root / "memory/knowledge/patterns_history.jsonl"
        present = self.reader.history("knowledge:patterns")["data"]["revision"]
        store.write_bytes(b"")
        empty = self.reader.history("knowledge:patterns")["data"]["revision"]
        store.unlink()
        absent = self.reader.history("knowledge:patterns")["data"]["revision"]
        self.assertEqual(3, len({present, empty, absent}))

    def test_graph_revision_moves_when_a_history_store_appears_or_is_deleted(self):
        store = self.root / "memory/knowledge/patterns_history.jsonl"
        pinned = self.reader.graph("knowledge:facts")["data"]["revision"]
        store.unlink()
        after_delete = self.reader.graph("knowledge:facts")["data"]["revision"]
        self.assertNotEqual(pinned, after_delete)
        # And the same store reappearing is equally a different corpus.
        self.write("memory/knowledge/patterns_history.jsonl", self.jline(
            {"ts": "9", "old_content": "restored old", "new_content": "restored new"}))
        self.assertNotEqual(after_delete,
                            self.reader.graph("knowledge:facts")["data"]["revision"])
        with self.assertRaises(AtlasError) as caught:
            self.reader.graph("knowledge:facts", revision=after_delete)
        self.assertEqual(caught.exception.code, "revision_drift")

    def test_graph_rejects_a_history_store_mutated_during_the_scan(self):
        # The store is stable when the scan starts and changed by the time it
        # ends: the after-read check has to reject the half-old edge set. The
        # store is appended to just after its bytes were taken for the opening
        # revision, so the scan really did run against the old content.
        original = self.reader._read_uncached
        store = self.root / "memory/scratchpad_journal.jsonl"
        mutated = []

        def mutating(path):
            raw = original(path)
            if path.name == store.name and not mutated:
                mutated.append(str(path))
                with store.open("a", encoding="utf-8") as handle:
                    handle.write(self.jline({"ts": "11", "type": "scratchpad_read"}))
            return raw

        self.reader._read_uncached = mutating
        try:
            with self.assertRaises(AtlasError) as caught:
                self.reader.graph("scratchpad")
        finally:
            self.reader._read_uncached = original
        self.assertEqual(caught.exception.code, "revision_drift")
        self.assertEqual(caught.exception.status, 409)

    def test_overlapping_requests_neither_share_nor_leak_a_read_phase(self):
        """Two requests overlap on one reader, and do not exit in order.

        ``plugin.py`` dispatches every request through ``asyncio.to_thread``,
        and the widget's compare view fires two of them at once, so two read
        phases are open on the same reader at the same time. Held on the
        reader, the phase entered first can be restored last: one request's
        bytes then answer the other's revision, and a populated cache stays
        installed with no phase open — exactly where the closing drift check
        reads, the one read that has to reach the file for real.
        """
        # Resolved, so a probe read can be handed straight to ``_read_all``.
        store = self.reader.root / "memory/scratchpad_journal.jsonl"
        original = self.reader._read_uncached
        stale = store.read_bytes()
        first_read = threading.Event()   # A holds the store's pre-change bytes
        second_read = threading.Event()  # B holds the store's post-change bytes
        first_done = threading.Event()   # A's phase has closed
        blocked, failures, results = {}, [], {}

        def hooked(path):
            raw = original(path)
            name = threading.current_thread().name
            if (path.name == store.name and name in ("atlas-a", "atlas-b")
                    and name not in blocked):
                blocked[name] = True
                if name == "atlas-a":
                    first_read.set()
                    if not second_read.wait(30):
                        failures.append("B never entered A's phase")
                else:
                    second_read.set()
                    if not first_done.wait(30):
                        failures.append("A's phase never closed")
            return raw

        def run(name):
            try:
                results[name] = self.reader.graph("scratchpad")["data"]["revision"]
            except AtlasError as exc:
                results[name] = exc
            finally:
                if name == "atlas-a":
                    first_done.set()

        self.reader._read_uncached = hooked
        a = threading.Thread(target=run, args=("atlas-a",), name="atlas-a")
        b = threading.Thread(target=run, args=("atlas-b",), name="atlas-b")
        try:
            a.start()
            self.assertTrue(first_read.wait(30), "A never read the store")
            # Mutated between the two requests, while A's phase is still open.
            with store.open("a", encoding="utf-8") as handle:
                handle.write(self.jline({"ts": "11", "type": "scratchpad_read"}))
            fresh = store.read_bytes()
            b.start()
            a.join(60)
            b.join(60)
        finally:
            first_read.set(), second_read.set(), first_done.set()
            for thread in (a, b):
                if thread.ident is not None:
                    thread.join(60)
            self.reader._read_uncached = original
        self.assertEqual(failures, [])
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(sorted(blocked), ["atlas-a", "atlas-b"],
                         "the two phases never overlapped")
        # (c) A read the store before the change and re-reads it after: its
        # closing check has to reject the half-old edge set.
        self.assertIsInstance(results["atlas-a"], AtlasError)
        self.assertEqual(results["atlas-a"].code, "revision_drift")
        self.assertEqual(results["atlas-a"].status, 409)
        # (b) B read only post-change bytes, so A's cached bytes must not have
        # answered B's revision: B succeeds, on the digest of what is on disk.
        self.assertNotIsInstance(results["atlas-b"], AtlasError)
        self.assertEqual(results["atlas-b"],
                         self.reader.graph("scratchpad")["data"]["revision"])
        # (a) No cache outlives the phases: a read taken outside any phase —
        # where every closing drift check runs — still reaches the file.
        with store.open("a", encoding="utf-8") as handle:
            handle.write(self.jline({"ts": "12", "type": "scratchpad_read"}))
        newest = store.read_bytes()
        self.assertNotIn(newest, (stale, fresh))
        outside = {}
        probe = threading.Thread(
            target=lambda: outside.setdefault("raw", self.reader._read_all(store)))
        probe.start()
        probe.join(60)
        self.assertEqual(outside.get("raw"), newest,
                         "a read phase leaked past the request that opened it")
        self.assertEqual(self.reader._read_all(store), newest)

    def test_search_rejects_an_oversized_cursor_before_decoding_it(self):
        for token in ("A" * (MAX_CURSOR_CHARS + 1), "=" * (MAX_CURSOR_CHARS + 4)):
            with self.assertRaises(AtlasError) as caught:
                self.reader.search("sharedterm", cursor=token)
            self.assertEqual(caught.exception.code, "invalid_cursor")
            self.assertEqual(caught.exception.status, 400)
        # The bound is on the token, not on legitimate paging.
        self.assertEqual(self.reader.search("sharedterm", limit=1)["data"]["items"][0]["id"],
                         self.reader.search("sharedterm")["data"]["items"][0]["id"])

    def test_search_rejects_an_offset_past_the_stated_maximum(self):
        revision = self.reader.search("sharedterm")["data"]["revision"]
        binding = "sharedterm\0False"
        beyond = self.reader._make_cursor("search", revision, binding,
                                          MAX_SEARCH_OFFSET + 1)
        with self.assertRaises(AtlasError) as caught:
            self.reader.search("sharedterm", cursor=beyond, limit=1)
        self.assertEqual(caught.exception.code, "invalid_cursor")
        self.assertEqual(caught.exception.status, 400)

    def test_search_pages_deep_without_retaining_earlier_matches(self):
        self.write("memory/knowledge/many.md",
                   "".join("line %d needleword\n" % n for n in range(1, 501)))
        revision = self.reader.search("needleword", limit=1)["data"]["revision"]
        binding = "needleword\0False"
        deep = self.reader.search(
            "needleword", limit=5,
            cursor=self.reader._make_cursor("search", revision, binding, 400))["data"]
        self.assertEqual([hit["line"] for hit in deep["items"]], [401, 402, 403, 404, 405])
        self.assertTrue(all(hit["id"] == "knowledge:many" for hit in deep["items"]))
        self.assertIsNotNone(deep["next_cursor"])
        # Regression guard for the shape of the fix: the page is accumulated
        # directly, never sliced out of an offset-sized list of every match.
        self.assertNotIn("hits[offset:", (PAYLOAD / "memory_reader.py").read_text("utf-8"))

    def test_search_reports_a_gap_instead_of_an_unusable_next_cursor(self):
        self.write("memory/knowledge/many.md",
                   "".join("line %d needleword\n" % n
                           for n in range(1, MAX_SEARCH_OFFSET + 3)))
        revision = self.reader.search("needleword", limit=1)["data"]["revision"]
        binding = "needleword\0False"
        capped = self.reader.search(
            "needleword", limit=1,
            cursor=self.reader._make_cursor("search", revision, binding,
                                            MAX_SEARCH_OFFSET))
        self.assertEqual([hit["line"] for hit in capped["data"]["items"]],
                         [MAX_SEARCH_OFFSET + 1])
        # More matches exist, but the cursor that would reach them could not be
        # spent, so the truncation is stated rather than handed out as a token.
        self.assertIsNone(capped["data"]["next_cursor"])
        self.assertTrue(any(g["scope"] == "search" and g["reason"] == "page_limit_reached"
                            for g in capped["gaps"]))

    def test_search_later_pages_keep_original_unicode_spans(self):
        self.write("memory/knowledge/many.md",
                   "".join("row %d Straße tail\n" % n for n in range(1, 11)))
        whole = self.reader.search("STRASSE", limit=100)["data"]["items"]
        paged, cursor, revision = [], None, None
        while True:
            page = self.reader.search("STRASSE", limit=3, cursor=cursor,
                                      revision=revision)["data"]
            paged.extend(page["items"])
            cursor, revision = page["next_cursor"], page["revision"]
            self.assertLessEqual(len(page["items"]), 3)
            if not cursor:
                break
        self.assertEqual(paged, whole)
        for hit in paged:
            self.assertEqual(hit["excerpt"][hit["match_start"]:hit["match_end"]], "Straße")

    def test_graph_rejects_the_removed_inferred_parameter(self):
        with self.assertRaises(TypeError):
            self.reader.graph("knowledge:facts", inferred=True)
        self.assertNotIn("inferred", (PAYLOAD / "memory_reader.py").read_text("utf-8"))
        self.assertNotIn("inferred", (PAYLOAD / "plugin.py").read_text("utf-8"))

    def test_bounds_oversize_history_is_gap_and_zero_writes(self):
        before = self.snapshot()
        (self.root / "memory/identity_journal.jsonl").write_bytes(b"x" * (MAX_FILE + 1))
        result = self.reader.history("identity")
        self.assertTrue(any(g["reason"] == "file_too_large" for g in result["gaps"]))
        self.assertEqual(self.snapshot()["memory/identity_journal.jsonl"][1], MAX_FILE + 1)
        self.assertEqual(before["memory/identity.md"], self.snapshot()["memory/identity.md"])

    def test_all_reader_operations_leave_source_tree_unchanged(self):
        before = self.snapshot()
        self.reader.catalog()
        self.reader.document("identity")
        history = self.reader.history("identity")["data"]["items"]
        snapshot = next(event for event in history if event["representation"] == "snapshot")
        self.reader.history_event("identity", snapshot["event_id"])
        self.reader.search("sharedterm")
        self.reader.graph("knowledge:facts")
        self.reader.graph("scratchpad")
        self.reader.dialogue()
        self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_traversal_and_symlink_swap_fail_closed(self):
        target = self.root / "outside.md"
        target.write_text("private", encoding="utf-8")
        os.symlink(target, self.root / "memory/knowledge/link.md")
        self.assertNotIn("knowledge:link", [x["id"] for x in self.reader.catalog()["data"]["items"]])
        with self.assertRaises(AtlasError):
            self.reader.document("../outside")
        source, _ = self.reader._source("knowledge:facts")
        source.path.unlink()
        os.symlink(target, source.path)
        with self.assertRaises(AtlasError):
            self.reader._read_all(source.path)

    def test_malformed_utf8_is_not_replacement_complete(self):
        (self.root / "memory/knowledge/facts.md").write_bytes(b"good\xffbad")
        with self.assertRaises(AtlasError) as caught:
            self.reader.document("knowledge:facts")
        self.assertEqual(caught.exception.code, "invalid_utf8")

    # ---- read budget and closing drift checks --------------------------
    @contextlib.contextmanager
    def read_budget(self, *, files=None, byte_limit=None):
        """Shrink the cumulative per-request read budget for one call.

        ``getattr`` defaults rather than direct attribute reads, so a build
        without the budget still runs the assertions below and fails on the
        volume it reads instead of on a missing name.
        """
        saved_files = getattr(memory_reader, "MAX_READ_FILES", None)
        saved_bytes = getattr(memory_reader, "MAX_READ_BYTES", None)
        if files is not None:
            memory_reader.MAX_READ_FILES = files
        if byte_limit is not None:
            memory_reader.MAX_READ_BYTES = byte_limit
        try:
            yield
        finally:
            if saved_files is not None:
                memory_reader.MAX_READ_FILES = saved_files
            if saved_bytes is not None:
                memory_reader.MAX_READ_BYTES = saved_bytes

    @contextlib.contextmanager
    def recording_reads(self):
        """Record every file this reader actually opens, and how big it was."""
        original = self.reader._read_uncached
        reads = []

        def hooked(path):
            raw = original(path)
            reads.append((str(path), len(raw)))
            return raw

        self.reader._read_uncached = hooked
        try:
            yield reads
        finally:
            self.reader._read_uncached = original

    @contextlib.contextmanager
    def mutating_read(self, rel, extra="\nappended after the opening read\n"):
        """Change a file just after its bytes are taken for the opening phase."""
        # Resolved: the reader canonicalises its root, so an unresolved path
        # would never match the path the read hook is handed.
        path = self.reader.root / rel
        original = self.reader._read_uncached
        touched = []

        def hooked(target):
            raw = original(target)
            if str(target) == str(path) and not touched:
                touched.append(str(target))
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(extra)
            return raw

        self.reader._read_uncached = hooked
        try:
            yield touched
        finally:
            self.reader._read_uncached = original

    def fill_sources(self, count=40, size=4096):
        """Add ``count`` knowledge topics that sort after the fixture's own."""
        for index in range(count):
            self.write(f"memory/knowledge/zfill-{index:02d}.md", "z" * size)

    def test_catalog_rejects_a_source_mutated_while_the_page_was_built(self):
        # catalog computed its revision and its items inside the read phase and
        # then returned, with no closing check at all — the one route missing
        # the check history, search, graph and dialogue all perform. A source
        # changed after its opening read could therefore be served as a mixed
        # page, and its cursor handed out bound to a revision that no longer
        # described the corpus. Both a source the page itself reads (its bytes
        # reused from the phase cache) and a source read only for the revision
        # must be caught.
        # The catalogue is ordered by id, so with limit=1 ``deep_review`` is the
        # one source the page itself reads (its item reuses the phase's cached
        # bytes) and ``knowledge:patterns`` is read only to build the revision.
        self.assertEqual(self.reader.catalog(limit=1)["data"]["items"][0]["id"],
                         "deep_review")
        for rel in ("memory/deep_review.md", "memory/knowledge/patterns.md"):
            with self.subTest(mutated=rel):
                with self.mutating_read(rel) as touched:
                    with self.assertRaises(AtlasError) as caught:
                        self.reader.catalog(limit=1)
                self.assertEqual(touched, [str(self.reader.root / rel)])
                self.assertEqual(caught.exception.code, "revision_drift")
                self.assertEqual(caught.exception.status, 409)

    def test_catalog_still_rejects_drift_inside_a_budget_bounded_corpus(self):
        # With the corpus larger than the request may read, the revision covers
        # a bounded prefix — and the closing check still has to reject a change
        # to a source inside that prefix.
        self.fill_sources()
        with self.read_budget(byte_limit=32 * 1024):
            with self.mutating_read("memory/deep_review.md"):
                with self.assertRaises(AtlasError) as caught:
                    self.reader.catalog(limit=1)
        self.assertEqual(caught.exception.code, "revision_drift")
        self.assertEqual(caught.exception.status, 409)

    def test_catalog_revision_work_stays_inside_the_read_budget(self):
        # _revision called _read_all for every entry while the phase budget
        # capped only which bytes were *retained*: once it was spent, reads kept
        # opening and hashing, so a catalog could hash MAX_SOURCES files of up
        # to MAX_FILE each — 2,000 x 4 MiB — behind an 8 MiB claim.
        self.fill_sources()
        total = sum(p.lstat().st_size for p in self.root.rglob("*") if p.is_file())
        byte_limit = 32 * 1024
        self.assertGreater(total, 4 * byte_limit, "the corpus must exceed the budget")
        with self.read_budget(byte_limit=byte_limit), self.recording_reads() as reads:
            result = self.reader.catalog(limit=200)
        # One opening phase and one closing drift-check phase, each bounded.
        self.assertLessEqual(sum(size for _, size in reads), 2 * byte_limit)
        gap = next(g for g in result["gaps"]
                   if g["scope"] == "catalog" and g["reason"] == "revision_scan_limit")
        # Part of the corpus, and the response says how much of it is missing.
        self.assertTrue(0 < gap["count"] < len(self.reader._sources()[0]))

    def test_catalog_file_count_stays_inside_the_read_budget(self):
        self.fill_sources()
        with self.read_budget(files=3), self.recording_reads() as reads:
            result = self.reader.catalog(limit=200)
        self.assertLessEqual(len(reads), 2 * 3)
        self.assertTrue(any(g["scope"] == "catalog"
                            and g["reason"] == "revision_scan_limit"
                            for g in result["gaps"]))

    def test_search_revision_work_stays_inside_the_scan_budget(self):
        # search hashed all MAX_SEARCH_FILES selected files before its byte
        # bound was ever consulted, so the advertised 8 MiB scan opened and
        # hashed the whole selection first.
        self.fill_sources()
        byte_limit = 32 * 1024
        with self.read_budget(byte_limit=byte_limit), self.recording_reads() as reads:
            result = self.reader.search("sharedterm")
        self.assertLessEqual(sum(size for _, size in reads), 2 * byte_limit)
        self.assertTrue(any(g["scope"] == "search" and g["reason"] == "byte_scan_limit"
                            for g in result["gaps"]))
        # The bounded corpus is still searched, not abandoned.
        self.assertTrue(result["data"]["items"])

    def test_graph_revision_work_stays_inside_the_read_budget(self):
        # The graph revision covers every discovered source *and* every history
        # store; hashing all of them before the 60-store / 8 MiB scan budget
        # applied made that budget an advertisement rather than a bound.
        self.fill_sources()
        byte_limit = 32 * 1024
        with self.read_budget(byte_limit=byte_limit), self.recording_reads() as reads:
            result = self.reader.graph("knowledge:facts")
        self.assertLessEqual(sum(size for _, size in reads), 2 * byte_limit)
        self.assertTrue(any(g["scope"] == "graph"
                            and g["reason"] == "revision_scan_limit"
                            for g in result["gaps"]))
        self.assertEqual(result["data"]["focus"], "knowledge:facts")

    def test_a_bounded_corpus_is_disclosed_and_never_served_as_complete(self):
        """A revision is either complete for its corpus, or the shortfall is stated.

        Two disclosed outcomes, no third silent one: a route that may serve part
        of a corpus names the entries it did not cover, and a route that cannot
        build its revision at all fails with a typed 413 rather than digesting
        whatever fitted.
        """
        self.fill_sources()
        with self.read_budget(byte_limit=32 * 1024):
            result = self.reader.catalog(limit=200)
            gap = next(g for g in result["gaps"]
                       if g["scope"] == "catalog"
                       and g["reason"] == "revision_scan_limit")
            self.assertGreater(gap["count"], 0)
            # Items outside the covered corpus carry no invented digest either.
            uncovered = [i for i in result["data"]["items"]
                         if i["read_error"] == "read_budget_exhausted"]
            self.assertTrue(uncovered)
            self.assertTrue(all(i["revision"] is None for i in uncovered))
            # A source the revision does not cover cannot move it: that is
            # exactly what the gap discloses.
            covered_revision = self.reader.catalog(limit=1)["data"]["items"]
            self.assertTrue(covered_revision)
        # And where no bounded prefix is meaningful — history binds one document
        # and its stores — the shortfall is a typed refusal, not a digest over
        # part of them.
        with self.read_budget(byte_limit=1):
            with self.assertRaises(AtlasError) as caught:
                self.reader.history("identity")
        self.assertEqual(caught.exception.code, "read_budget_exhausted")
        self.assertEqual(caught.exception.status, 413)

    # ---- the read set is the revision set (mixed-size corpora) ---------
    #
    # A budget that stops at the first file too big to fit still has bytes
    # left over. While the selection was only a *prefix* of the corpus, those
    # leftover bytes let a later, smaller file be read for the response body
    # after the revision had already been fixed without it: the answer then
    # rested on bytes its own revision did not cover, so mutating them left
    # the pinned revision unchanged. The selection is a set now, and reads
    # outside it are refused, so the two sets are one.

    def budget_for(self, paths, slack):
        """A byte budget that admits every path in ``paths``, plus ``slack``."""
        total = 0
        for path in paths:
            try:
                total += path.lstat().st_size
            except OSError:
                pass
        return total + slack

    SMALL_LATER = 256
    TOO_BIG = 128 * 1024

    def mixed_size_catalog_budget(self):
        """A corpus where a non-fitting source is followed by a smaller one."""
        slack = 8 * 1024
        self.assertGreater(self.TOO_BIG, slack, "the big source must not fit")
        self.assertLess(self.SMALL_LATER, slack, "the later source must fit alone")
        base = self.budget_for([s.path for s in self.reader._sources()[0]], slack)
        # ``zz-big`` sorts before ``zz-small``, and both after every fixture id.
        self.write("memory/knowledge/zz-big.md", "b" * self.TOO_BIG)
        self.write("memory/knowledge/zz-small.md", "s" * self.SMALL_LATER)
        return base

    def test_catalog_never_reads_a_small_source_past_a_source_that_did_not_fit(self):
        byte_limit = self.mixed_size_catalog_budget()
        with self.read_budget(byte_limit=byte_limit), self.recording_reads() as reads:
            result = self.reader.catalog(limit=200)
        opened = {path for path, _ in reads}
        # (a) The smaller later source is not read into the response at all.
        self.assertNotIn(str(self.reader.root / "memory/knowledge/zz-small.md"), opened)
        self.assertNotIn(str(self.reader.root / "memory/knowledge/zz-big.md"), opened)
        items = {item["id"]: item for item in result["data"]["items"]}
        # (b) And its exclusion is disclosed, on the item and in the gaps.
        for source_id in ("knowledge:zz-big", "knowledge:zz-small"):
            self.assertEqual(items[source_id]["read_error"], "read_budget_exhausted")
            self.assertIsNone(items[source_id]["revision"])
            self.assertTrue(any(g["scope"] == source_id
                                and g["reason"] == "read_budget_exhausted"
                                for g in result["gaps"]))
        gap = next(g for g in result["gaps"] if g["scope"] == "catalog"
                   and g["reason"] == "revision_scan_limit")
        self.assertGreaterEqual(gap["count"], 2)

    def test_catalog_still_drifts_on_any_source_the_page_actually_read(self):
        # (c) Every file the response did read is covered by the revision it is
        # served under, so changing any of them is caught by the closing check.
        byte_limit = self.mixed_size_catalog_budget()
        for rel in ("memory/deep_review.md", "memory/knowledge/patterns.md"):
            with self.subTest(mutated=rel):
                with self.read_budget(byte_limit=byte_limit):
                    with self.mutating_read(rel) as touched:
                        with self.assertRaises(AtlasError) as caught:
                            self.reader.catalog(limit=200)
                self.assertTrue(touched)
                self.assertEqual(caught.exception.code, "revision_drift")
                self.assertEqual(caught.exception.status, 409)

    def mixed_size_graph_budget(self):
        """A graph corpus where a non-fitting source precedes the focus stores.

        ``knowledge:zz-big`` sorts before ``scratchpad``, so the focus document
        and its two stores all fall after the entry that does not fit. The
        focus is required and stays in the set; its stores do not, and the
        journal edge they would have produced is dropped and disclosed rather
        than served under a revision that does not cover the journal.
        """
        slack = 8 * 1024
        entries = self.reader._graph_entries(self.reader._sources()[0])
        base = self.budget_for([path for _, path in entries], slack)
        for rel in ("memory/scratchpad.md", "memory/scratchpad_blocks.json",
                    "memory/scratchpad_journal.jsonl"):
            self.assertLess((self.root / rel).lstat().st_size, slack,
                            "the focus and its stores must each fit in the slack")
        self.write("memory/knowledge/zz-big.md", "b" * self.TOO_BIG)
        return base

    def test_graph_never_reads_a_store_past_an_entry_that_did_not_fit(self):
        byte_limit = self.mixed_size_graph_budget()
        with self.read_budget(byte_limit=byte_limit), self.recording_reads() as reads:
            result = self.reader.graph("scratchpad")
        opened = {path for path, _ in reads}
        # (a) The smaller stores after the non-fitting entry are never opened,
        # even though the budget still had room for them.
        for rel in ("memory/knowledge/zz-big.md", "memory/scratchpad_blocks.json",
                    "memory/scratchpad_journal.jsonl"):
            self.assertNotIn(str(self.reader.root / rel), opened)
        # The focus is a mandatory member, so it is read and it is covered.
        self.assertIn(str(self.reader.root / "memory/scratchpad.md"), opened)
        self.assertEqual(result["data"]["focus"], "scratchpad")
        # (b) Both omissions are disclosed: entries outside the revision, and a
        # scan that could not read every store it would have consulted.
        self.assertTrue(any(g["scope"] == "graph"
                            and g["reason"] == "revision_scan_limit"
                            for g in result["gaps"]))
        self.assertTrue(any(g["scope"] == "graph" and g["reason"] == "scan_limit"
                            for g in result["gaps"]))
        # The journal-derived edge rested on an unselected file, so it is not
        # served; only edges provable from the selected set remain.
        self.assertFalse([e for e in result["data"]["edges"]
                          if e["kind"] == "journal_source_ref"])
        unbounded = self.reader.graph("scratchpad")["data"]["edges"]
        self.assertTrue([e for e in unbounded if e["kind"] == "journal_source_ref"])

    def test_graph_revision_covers_exactly_the_files_the_response_read(self):
        byte_limit = self.mixed_size_graph_budget()
        with self.read_budget(byte_limit=byte_limit), self.recording_reads() as reads:
            result = self.reader.graph("scratchpad")
        opened = {path for path, _ in reads}
        entries = self.reader._graph_entries(self.reader._sources()[0])
        covered = [(name, path) for name, path in entries if str(path) in opened]
        # Nothing was read that is not a graph entry, and the served digest is
        # the digest of exactly the files that were read.
        self.assertEqual(opened, {str(path) for _, path in covered})
        self.assertEqual(result["data"]["revision"], self.reader._revision(covered))

    def test_graph_focus_after_a_non_fitting_entry_is_still_covered(self):
        # (c) for graph: the focus sorts after the entry that did not fit, and
        # mutating it is still caught — it is a required member of the set, so
        # the revision it is served under covers its bytes.
        byte_limit = self.mixed_size_graph_budget()
        with self.read_budget(byte_limit=byte_limit):
            with self.mutating_read("memory/scratchpad.md") as touched:
                with self.assertRaises(AtlasError) as caught:
                    self.reader.graph("scratchpad")
        self.assertTrue(touched)
        self.assertEqual(caught.exception.code, "revision_drift")
        self.assertEqual(caught.exception.status, 409)

    def test_graph_focus_that_does_not_fit_is_a_typed_bounded_outcome(self):
        # (d) A focus the request cannot read is a 413, not a graph answered
        # from everything except the document it is supposed to be about.
        self.write("memory/knowledge/facts.md", "f" * self.TOO_BIG)
        with self.read_budget(byte_limit=16 * 1024):
            with self.assertRaises(AtlasError) as caught:
                self.reader.graph("knowledge:facts")
        self.assertEqual(caught.exception.code, "read_budget_exhausted")
        self.assertEqual(caught.exception.status, 413)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mode, p.lstat().st_size,
                                                p.lstat().st_mtime_ns)
                for p in self.root.rglob("*")}


class PluginTests(unittest.TestCase):
    def test_real_registration_and_all_seven_request_handlers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "memory/knowledge").mkdir(parents=True)
            (root / "memory/dialogue_blocks.json").write_text(
                json.dumps([{"ts": "1", "type": "summary", "content": "block"}]),
                encoding="utf-8")
            (root / "memory/identity.md").write_text("identity", encoding="utf-8")
            (root / "memory/identity_journal.jsonl").write_text(
                json.dumps({"ts": "1", "old_content": "old", "new_content": "new"}) + "\n")
            class API:
                def __init__(self): self.routes, self.tab, self.unload = {}, None, None
                def get_runtime_info(self): return {"data_dir": td}
                def register_route(self, name, *, handler, methods):
                    self.routes[name] = (handler, methods)
                def register_ui_tab(self, tab_id, *, title, icon, render):
                    self.tab = (tab_id, title, icon, render)
                def on_unload(self, callback): self.unload = callback
            api = API()
            plugin = MemoryAtlasPlugin()
            plugin.register(api)
            self.assertEqual(set(api.routes), {"catalog", "document", "history", "history/event",
                                               "search", "graph", "dialogue"})
            self.assertEqual(api.tab[2], "◈")
            self.assertEqual(api.tab[3]["kind"], "module")
            self.assertEqual(api.tab[3]["entry"], "widget.js")
            self.assertEqual(api.tab[3]["height"], 580)
            self.assertEqual(api.tab[3]["span"], 2)
            self.assertEqual(api.tab[3]["start"], "manual")
            async def call(name, query):
                scope = {"type": "http", "method": "GET", "path": "/" + name,
                         "query_string": query.encode(), "headers": []}
                return await api.routes[name][0](Request(scope))
            caller = threading.get_ident()
            seen = []
            original = plugin.dispatch
            def wrapped(*args, **kwargs):
                seen.append(threading.get_ident() != caller)
                return original(*args, **kwargs)
            plugin.dispatch = wrapped
            catalog = asyncio.run(call("catalog", "")); self.assertEqual(catalog.status_code, 200)
            self.assertEqual(seen, [True])
            document = asyncio.run(call("document", "id=identity")); self.assertEqual(document.status_code, 200)
            history = asyncio.run(call("history", "id=identity")); self.assertEqual(history.status_code, 200)
            event_id = json.loads(history.body)["data"]["items"][0]["event_id"]
            event = asyncio.run(call("history/event", "id=identity&event_id=" + event_id)); self.assertEqual(event.status_code, 200)
            search = asyncio.run(call("search", "q=identity")); self.assertEqual(search.status_code, 200)
            graph = asyncio.run(call("graph", "focus=identity")); self.assertEqual(graph.status_code, 200)
            self.assertEqual(graph.headers["cache-control"], "no-store")
            dialogue = asyncio.run(call("dialogue", "limit=5"))
            self.assertEqual(dialogue.status_code, 200)
            self.assertEqual(dialogue.headers["cache-control"], "no-store")
            body = json.loads(dialogue.body)["data"]
            self.assertEqual([b["type"] for b in body["blocks"]], ["summary"])
            self.assertFalse(body["meta"]["available"])
            self.assertEqual(body["meta"]["reason"], "missing")
            # "inferred" is gone: the unknown-parameter check now rejects it.
            rejected = asyncio.run(call("graph", "focus=identity&inferred=true"))
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(json.loads(rejected.body)["error"]["code"],
                             "unknown_parameter")
            bad = asyncio.run(call("dialogue", "focus=identity"))
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(json.loads(bad.body)["error"]["code"],
                             "unknown_parameter")
            # A non-finite number in a source record must not become a 500.
            # JSONResponse serializes with allow_nan=False, so a NaN or an
            # Infinity that json.loads happily accepted used to break the whole
            # route; it is reported as a malformed_record gap instead.
            (root / "memory/dialogue_blocks.json").write_text(
                '[{"ts":"1","type":"summary","content":"nan","message_count":NaN},'
                ' {"ts":"2","type":"summary","content":"inf","range":[0,Infinity]},'
                ' {"ts":"3","type":"summary","content":"kept"}]',
                encoding="utf-8")
            nonfinite = asyncio.run(call("dialogue", "limit=5"))
            self.assertEqual(nonfinite.status_code, 200)
            payload = json.loads(nonfinite.body)
            self.assertEqual([b["content"] for b in payload["data"]["blocks"]],
                             ["kept"])
            self.assertEqual(sum(g["count"] for g in payload["gaps"]
                                 if g["reason"] == "malformed_record"), 2)
            (root / "memory/identity_journal.jsonl").write_text(
                '{"ts":"1","old_content":"a","new_content":"b","rounds":Infinity}\n'
                '{"ts":"2","old_content":"b","new_content":"c"}\n', encoding="utf-8")
            infinite = asyncio.run(call("history", "id=identity"))
            self.assertEqual(infinite.status_code, 200)
            self.assertTrue(any(g["reason"] == "malformed_record"
                                for g in json.loads(infinite.body)["gaps"]))
            for field in ("last_consolidated_offset", "chat_log_signature",
                          "last_consolidated_at"):
                (root / "memory/dialogue_meta.json").write_text(
                    '{"%s":"\\ud800"}' % field, encoding="utf-8")
                unsafe_meta = asyncio.run(call("dialogue", "limit=5"))
                self.assertEqual(unsafe_meta.status_code, 200, field)
                meta = json.loads(unsafe_meta.body)["data"]["meta"]
                self.assertFalse(meta["available"], field)
                self.assertEqual(meta["reason"], "malformed_json", field)
            api.unload(); self.assertIsNone(plugin.reader)


if __name__ == "__main__":
    unittest.main()
