"""Kanban contracts through the registered routes and tools, using synthetic state."""
import copy
import json
import threading
from unittest import mock

from test_smart_lists import SkillCase, downgrade_entries


class KanbanTests(SkillCase):
    def setUp(self):
        super().setUp()
        self.ok("group", action="create", name="Work")

    def create(self, text="Original", **fields):
        return self.ok("add", group="Work", request_id=f"add-{text}",
                       items=[{"text": text, **fields}])["added"][0]["id"]

    def test_schema_three_migration_keeps_records_and_replay(self):
        eid = self.create("old", details={"tags": ["legacy"], "references": [
            {"label": "source", "target": "https://example.org/thread"}]})
        self.ok("complete", entry_ids=[eid], request_id="old-done")
        doc = downgrade_entries(json.loads(self.store_bytes()), 3)
        before = copy.deepcopy(doc)
        self.state_dir.joinpath("store.json").write_text(json.dumps(doc))
        self.mark_fixture_as_legacy_store()
        raw = self.store_bytes()
        read = self.ok("entry", entry_id=eid)
        self.assertEqual(read["entry"]["stage"], "done")
        self.assertEqual(raw, self.store_bytes(), "reads may migrate in memory only")
        self.ok("update", entry_id=eid, description="new context")
        current = json.loads(self.store_bytes())
        self.assertEqual(current["schema_version"], 5)
        self.assertEqual(current["entries"][eid]["text"], before["entries"][eid]["text"])
        self.assertEqual(current["entries"][eid]["details"], before["entries"][eid]["details"])
        self.assertEqual(current["entries"][eid]["description"], "new context")
        self.assertNotIn("status", current["entries"][eid])
        self.assertEqual(current["requests"]["old-done"], before["requests"]["old-done"])

    def test_four_columns_filter_before_independent_paging(self):
        for stage in ("todo", "in_progress", "review", "done"):
            for batch in range(2):
                self.ok("add", group="Work", request_id=f"{stage}-{batch}", items=[
                    {"text": f"{stage}-{batch}-{i}", "stage": stage,
                     "details": {"tags": ["chosen" if i % 2 else "other"]}} for i in range(60)])
        board = self.api.call_route("board", "GET", query={"tag": "chosen", "offset_todo": "10",
                                                           "offset_review": "50", "limit": "15"})
        self.assertTrue(board["ok"], board)
        self.assertEqual(board["selected_tags"], ["chosen"])
        self.assertEqual({stage: row["total"] for stage, row in board["stages"].items()},
                         dict.fromkeys(("todo", "in_progress", "review", "done"), 60))
        self.assertEqual([len(board["stages"][stage]["entries"]) for stage in
                          ("todo", "in_progress", "review", "done")], [15, 15, 10, 15])
        self.assertEqual(board["stages"]["review"]["next_offset"], 60)
        self.assertEqual(board["stages"]["todo"]["entries"][0]["text"], "todo-0-21")

    def test_archive_is_separate_from_done_and_trash(self):
        eid = self.create("one", stage="review")
        entry = self.api.call_route("entry", "GET", query={"entry_id": eid})["entry"]
        archived = self.api.call_route("archive", "POST", {"entry_id": eid, "archived": True,
            "expected": {"archived_at": None}, "request_id": "archive-one"})
        self.assertTrue(archived["ok"], archived)
        self.assertEqual(archived["entry"]["stage"], "review")
        self.assertEqual(self.api.call_route("board", "GET")["stages"]["review"]["total"], 0)
        self.assertEqual(self.api.call_route("archive", "GET")["total"], 1)
        self.assertEqual(self.ok("read", archive="archived")["total"], 1)
        self.assertEqual(self.ok("read")["total"], 1, "legacy read includes archive")
        self.assertTrue(self.api.call_route("archive", "POST", {"entry_id": eid, "archived": True,
            "expected": {"archived_at": None}, "request_id": "archive-one"})["replayed"])
        self.ok("delete", entry_ids=[eid])
        self.assertEqual(self.api.call_route("archive", "GET")["total"], 0)
        self.ok("delete", entry_ids=[eid], action="undo")
        self.assertEqual(self.api.call_route("archive", "GET")["total"], 1)
        restored = self.api.call_route("archive", "POST", {"entry_id": eid, "archived": False,
            "expected": {"archived_at": archived["entry"]["archived_at"]}, "request_id": "restore-one"})
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(self.api.call_route("board", "GET")["stages"]["review"]["total"], 1)
        self.ok("complete", entry_ids=[eid], done=True)
        self.assertEqual(self.ok("entry", entry_id=eid)["entry"]["stage"], "done")
        self.ok("complete", entry_ids=[eid], done=False)
        self.assertEqual(self.ok("entry", entry_id=eid)["entry"]["stage"], "todo")

    def test_full_entry_agent_pages_and_export_restore_preserve_links(self):
        refs = [{"label": f"Thread {i}", "target": f"https://example.org/{i}/" + "x" * 200}
                for i in range(100)]
        description = "Context " + "D" * (16000 - len("Context "))
        eid = self.create("large", description=description, details={"references": refs, "note": "evidence"})
        route = self.api.call_route("entry", "GET", query={"entry_id": eid})
        self.assertEqual(route["entry"]["description"], description)
        self.assertEqual(route["entry"]["details"]["references"], refs)
        self.assertEqual(self.ok("entry", entry_id=eid)["reference_count"], 100)
        before = self.store_bytes()
        self.refused("update", "invalid_input", entry_id=eid,
                     details={"references": refs + [{"label": "extra", "target": "task:extra"}]})
        self.assertEqual(self.store_bytes(), before)
        for field, wanted in (("description", description), ("references", refs)):
            offset, assembled = 0, [] if field == "references" else ""
            while True:
                page = self.ok("entry", entry_id=eid, field=field, offset=offset)
                assembled += page["value"]
                offset = page["next_offset"]
                if offset == page["total"]:
                    break
            self.assertEqual(assembled, wanted)
        export = self.ok("store", action="export")
        other = self.fresh_api(self.state_dir.parent / "copy")
        self.assertTrue(other.call_tool("store", action="restore", file=export["file"])["ok"])
        self.assertEqual(other.call_route("entry", "GET", query={"entry_id": eid})["entry"]["details"]["references"], refs)

    def test_maximum_unicode_fields_have_lossless_agent_pages_and_export(self):
        text = "😀" * 900 + "\x00" * 100
        due = "😀" * 120
        tags = [f"{i:02d}" + "😀" * 58 for i in range(20)]
        description = "😀" * 16000
        references = [{"label": "😀" * 120,
                       "target": "https://example.org/" + "😀" * (1000 - len("https://example.org/"))}
                      for _ in range(100)]
        details = {"tags": tags, "note": "😀" * 1000, "next_step": "😀" * 1000,
                   "capture_key": "😀" * 200, "references": references}
        eid = self.ok("add", group="Work", request_id="max-unicode", items=[{
            "text": text, "due": due, "description": description, "details": details}])["added"][0]["id"]
        overview = self.ok("entry", entry_id=eid)
        self.assertEqual(overview["entry"]["id"], eid)
        self.assertEqual(overview["text_length"], 1000)
        self.assertEqual(overview["tag_count"], 20)
        self.assertEqual(overview["reference_count"], 100)
        self.assertIsNone(overview["due_at"])
        for field, wanted in (("text", text), ("due", due), ("tags", tags),
                              ("description", description), ("references", references),
                              ("note", details["note"]), ("next_step", details["next_step"]),
                              ("capture_key", details["capture_key"]), ("group_path", "Work")):
            offset, assembled = 0, [] if isinstance(wanted, list) else ""
            while True:
                page = self.ok("entry", entry_id=eid, field=field, offset=offset)
                self.assertEqual(page["revision"], overview["revision"])
                assembled += page["value"]
                offset = page["next_offset"]
                if offset == page["total"]:
                    break
            self.assertEqual(assembled, wanted)
        exported = self.ok("store", action="export")
        other = self.fresh_api(self.state_dir.parent / "max-copy")
        self.assertTrue(other.call_tool("store", action="restore", file=exported["file"])["ok"])
        restored = other.call_route("entry", "GET", query={"entry_id": eid})["entry"]
        self.assertEqual(restored["text"], text)
        self.assertEqual(restored["description"], description)
        for key, value in details.items():
            self.assertEqual(restored["details"][key], value)

    def test_agent_pages_expose_revision_change_between_reads(self):
        eid = self.create("paged", description="A" * 1200)
        first = self.ok("entry", entry_id=eid, field="description", offset=0)
        self.ok("update", entry_id=eid, description="B" * 1200,
                expected={"description": "A" * 1200})
        second = self.ok("entry", entry_id=eid, field="description", offset=first["next_offset"])
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertNotEqual(first["value"], second["value"])

    def test_edit_fields_refuses_invalid_text_without_persisting(self):
        eid = self.create("editable")
        before = self.store_bytes()
        for invalid in (" " * 10, "😀" * 1001, "\ud800", ["text"]):
            result = self.api.call_route("edit", "POST", {"entry_id": eid,
                "changes": {"text": invalid}, "expected": {"text": "editable"},
                "request_id": "bad-text"})
            self.assertFalse(result["ok"], result)
            self.assertEqual(self.store_bytes(), before)

    def test_partial_expected_edits_merge_and_conflict_atomically(self):
        eid = self.create("two", description="old", details={"references": [
            {"label": "old", "target": "https://example.org/old"}]})
        first = self.api.call_route("edit", "POST", {"entry_id": eid,
            "changes": {"description": "edited"}, "expected": {"description": "old"}, "request_id": "edit-desc"})
        self.assertTrue(first["ok"], first)
        links = [{"label": "new", "target": "https://example.org/new"}]
        self.ok("update", entry_id=eid, details={"references": links},
                expected={"details": {"references": [{"label": "old", "target": "https://example.org/old"}]}})
        retry = self.api.call_route("edit", "POST", {"entry_id": eid,
            "changes": {"description": "edited"}, "expected": {"description": "old"}, "request_id": "edit-desc"})
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["entry"]["details"]["references"], links)
        rebound = self.api.call_route("edit", "POST", {"entry_id": eid,
            "changes": {"description": "edited"}, "expected": {"description": "edited"}, "request_id": "edit-desc"})
        self.assertEqual(rebound["code"], "request_conflict")
        stale = self.api.call_route("edit", "POST", {"entry_id": eid,
            "changes": {"description": "wrong", "stage": "done"},
            "expected": {"description": "old", "stage": "todo"}, "request_id": "stale"})
        self.assertEqual(stale["code"], "field_conflict")
        self.assertEqual(stale["conflicts"], ["description"])
        self.assertEqual(self.api.call_route("entry", "GET", query={"entry_id": eid})["entry"]["stage"], "todo")
        self.assertEqual(self.refused("update", "field_conflict", entry_id=eid, details={"references": []},
            expected={"details": {"references": [{"label": "old", "target": "https://example.org/old"}]}})["error"]["code"],
            "field_conflict")
        self.assertEqual(self.ok("entry", entry_id=eid, field="references")["value"], links)

    def test_store_size_refuses_write_before_touching_state(self):
        before = self.store_bytes()
        with mock.patch.object(self.module.lists_core, "MAX_STORE_BYTES", len(before)):
            self.refused("add", "limit", group="Work", request_id="too-large", items=[{"text": "new"}])
        self.assertEqual(self.store_bytes(), before)

    def test_two_store_instances_commit_independent_fields(self):
        eid = self.create("parallel", description="before", details={"turn": "owner"})
        first = self.module.lists_core.SmartLists(self.state_dir)
        second = self.module.lists_core.SmartLists(self.state_dir)
        start = threading.Barrier(3)
        results = []

        def edit(service, changes, expected, request_id):
            start.wait()
            results.append(service.edit_fields(eid, changes, expected, request_id=request_id))

        threads = [threading.Thread(target=edit, args=(first, {"description": "after"},
                          {"description": "before"}, "writer-description")),
                   threading.Thread(target=edit, args=(second, {"details": {"turn": "agent"}},
                          {"details": {"turn": "owner"}}, "writer-turn"))]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        entry = self.api.call_route("entry", "GET", query={"entry_id": eid})["entry"]
        self.assertEqual((entry["description"], entry["details"]["turn"]), ("after", "agent"))
