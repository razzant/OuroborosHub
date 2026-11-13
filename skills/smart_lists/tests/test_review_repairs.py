"""0.6.3 repairs: expected due shapes, HTTP(S) scheme case and packed agent pages."""
import json

from test_smart_lists import SkillCase


class ExpectedDueTests(SkillCase):
    """An agent passes the due value it read back as `expected`; that must not conflict by itself."""

    def setUp(self):
        super().setUp()
        self.ok("group", action="create", name="Work")

    def create(self, text, **fields):
        return self.ok("add", group="Work", request_id="add-" + text.replace(" ", "-"),
                       items=[{"text": text, **fields}])["added"][0]["id"]

    def edit(self, eid, changes, expected, request_id):
        return self.api.call_route("edit", "POST", {"entry_id": eid, "changes": changes,
                                                    "expected": expected, "request_id": request_id})

    def test_unset_due_read_back_through_every_reader_matches(self):
        eid = self.create("no due")
        page = self.ok("entry", entry_id=eid, field="due")
        self.assertEqual(page["value"], "")
        summary = self.ok("read", group="Work")["entries"][0]
        self.assertIsNone(summary["due"])
        for number, expected in enumerate((page["value"], summary["due"], None, "")):
            self.ok("update", entry_id=eid, due=f"day {number}", request_id=f"set-{number}",
                    expected={"due": self.ok("entry", entry_id=eid, field="due")["value"] if number else expected})
            self.ok("update", entry_id=eid, clear_due=True, request_id=f"clear-{number}",
                    expected={"due": f"day {number}"})
        self.assertIsNone(self.api.call_route("entry", "GET", query={"entry_id": eid})["entry"]["due"])
        # The widget's own POST edit with null and with the unset text
        self.assertTrue(self.edit(eid, {"due": "Friday"}, {"due": None}, "route-null")["ok"])
        self.assertTrue(self.edit(eid, {"due": None}, {"due": "Friday"}, "route-clear")["ok"])
        self.assertTrue(self.edit(eid, {"due": "Friday"}, {"due": ""}, "route-blank")["ok"])

    def test_set_due_object_from_read_matches_and_real_changes_still_conflict(self):
        eid = self.create("with due", due="Friday")
        summary = self.ok("read", group="Work")["entries"][0]
        self.assertEqual(summary["due"], {"raw": "Friday"})
        self.ok("update", entry_id=eid, due="Monday", request_id="from-read", expected={"due": summary["due"]})
        timed = self.ok("update", entry_id=eid, due="2026-11-20T18:00+03:00", request_id="from-page",
                        expected={"due": self.ok("entry", entry_id=eid, field="due")["value"]})
        self.assertTrue(timed["ok"])
        full = self.api.call_route("entry", "GET", query={"entry_id": eid})["entry"]
        self.assertEqual(full["due"], {"raw": "2026-11-20T18:00+03:00", "at": "2026-11-20T18:00:00+03:00"})
        # The complete stored object, with its parsed instant, is a declared representation too.
        self.assertTrue(self.edit(eid, {"due": "Sunday"}, {"due": full["due"]}, "route-object")["ok"])
        before = self.store_bytes()
        for number, stale in enumerate(("Friday", {"raw": "Monday"}, None, "", {"raw": "Sunday "})):
            self.refused("update", "field_conflict", entry_id=eid, due="Tuesday",
                         request_id=f"stale-{number}", expected={"due": stale})
            refused = self.edit(eid, {"due": "Tuesday"}, {"due": stale}, f"stale-route-{number}")
            self.assertEqual(refused["code"], "field_conflict", refused)
            self.assertEqual(refused["conflicts"], ["due"])
        self.assertEqual(self.store_bytes(), before)

    def test_invalid_expected_due_types_are_refused_not_conflicts(self):
        eid = self.create("typed", due="Friday")
        before = self.store_bytes()
        for number, invalid in enumerate((5, True, ["Friday"], {"raw": 5}, {"at": "x"}, {},
                                          {"raw": "Friday", "extra": 1}, {"raw": "Friday", "at": 3})):
            self.refused("update", "invalid_input", entry_id=eid, due="Monday",
                         request_id=f"typed-{number}", expected={"due": invalid})
            refused = self.edit(eid, {"due": "Monday"}, {"due": invalid}, f"typed-route-{number}")
            self.assertEqual(refused["code"], "invalid_input", refused)
        self.assertEqual(self.store_bytes(), before)

    def test_replay_after_due_change_is_still_identity_based(self):
        eid = self.create("replayed")
        first = self.edit(eid, {"due": "Friday"}, {"due": ""}, "once")
        self.assertTrue(first["ok"], first)
        again = self.edit(eid, {"due": "Friday"}, {"due": ""}, "once")
        self.assertTrue(again["replayed"])
        rebound = self.edit(eid, {"due": "Friday"}, {"due": None}, "once")
        self.assertEqual(rebound["code"], "request_conflict")


class ReferenceSchemeTests(SkillCase):
    def setUp(self):
        super().setUp()
        self.ok("group", action="create", name="Work")
        self.eid = self.ok("add", group="Work", request_id="seed",
                           items=[{"text": "links"}])["added"][0]["id"]

    def test_uppercase_scheme_is_stored_lowercase_and_the_rest_is_exact(self):
        given = [{"label": "Upper", "target": "HTTPS://Example.ORG/Path/%7Euser?Q=A&b=C#Frag"},
                 {"label": "Mixed", "target": "Http://example.org/A"},
                 {"label": "Plain", "target": "https://example.org/x"},
                 {"label": "Work", "target": "task:abc"}]
        self.ok("update", entry_id=self.eid, request_id="upper", details={"references": given},
                expected={"details": {"references": []}})
        stored = self.api.call_route("entry", "GET", query={"entry_id": self.eid})["entry"]["details"]["references"]
        self.assertEqual([ref["target"] for ref in stored], [
            "https://Example.ORG/Path/%7Euser?Q=A&b=C#Frag", "http://example.org/A",
            "https://example.org/x", "task:abc"])
        # The widget's route accepts the same shape.
        result = self.api.call_route("edit", "POST", {
            "entry_id": self.eid, "request_id": "route-upper",
            "changes": {"details": {"references": stored + [{"label": "R", "target": "HTTPS://example.org/route"}]}},
            "expected": {"details": {"references": stored}}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["entry"]["details"]["references"][-1]["target"], "https://example.org/route")

    def test_credentials_and_other_schemes_stay_refused_in_any_case(self):
        before = self.store_bytes()
        for number, target in enumerate(("HTTPS://user:pw@example.org/", "HTTP://user@example.org/",
                                         "HTTPS:///nohost", "FTP://example.org/", "HTTPS:example.org",
                                         "javascript:alert(1)", "https:/example.org")):
            self.refused("update", "invalid_input", entry_id=self.eid, request_id=f"bad-{number}",
                         details={"references": [{"label": "x", "target": target}]})
        self.assertEqual(self.store_bytes(), before)


class PackedAgentPageTests(SkillCase):
    def setUp(self):
        super().setUp()
        self.ok("group", action="create", name="Work")
        self.limit = self.module.MAX_TOOL_RESULT_BYTES

    def pages(self, eid, field):
        offset, pages = 0, []
        while True:
            raw = self.api.tools["entry"]["handler"](None, entry_id=eid, field=field, offset=offset)
            self.assertLessEqual(len(raw.encode("utf-8")), self.limit)
            page = json.loads(raw)
            self.assertTrue(page["ok"], page)
            self.assertEqual(page["offset"], offset)
            self.assertGreater(page["next_offset"], offset)
            self.assertEqual(page["next_offset"], offset + len(page["value"]))
            pages.append(page)
            offset = page["next_offset"]
            if offset == page["total"]:
                return pages

    def test_ordinary_tags_and_references_arrive_in_one_call(self):
        tags = [f"tag {i}" for i in range(20)]
        refs = [{"label": f"Thread {i}", "target": f"https://example.org/t/{i}"} for i in range(100)]
        eid = self.ok("add", group="Work", request_id="packed", items=[{
            "text": "packed", "details": {"tags": tags, "references": refs}}])["added"][0]["id"]
        self.assertEqual([page["value"] for page in self.pages(eid, "tags")], [tags])
        self.assertEqual([page["value"] for page in self.pages(eid, "references")], [refs])

    def test_worst_case_escapes_and_wide_characters_split_exactly(self):
        # Control characters in labels escape to six bytes; four-byte characters fill the targets.
        refs = [{"label": "\x01" * 60 + "😀" * 60,
                 "target": "https://example.org/" + "😀" * (1000 - len("https://example.org/"))}
                for _ in range(80)]  # 80 such links fill the 128,000-character details budget
        tags = ["\x02" * 30 + '"\\' * 15 for _ in range(1)] + [f"{i:02d}" + "😀" * 58 for i in range(19)]
        eid = self.ok("add", group="Work", request_id="worst", items=[{
            "text": "worst", "details": {"tags": tags, "references": refs}}])["added"][0]["id"]
        for field, wanted in (("references", refs), ("tags", tags)):
            pages = self.pages(eid, field)
            assembled = [item for page in pages for item in page["value"]]
            self.assertEqual(assembled, wanted)
            if field == "references":
                self.assertGreater(len(pages), 1)
                self.assertLess(len(pages), 80)

    def test_overview_reports_every_paged_length(self):
        eid = self.ok("add", group="Work", request_id="lengths", items=[{
            "text": "abc", "due": "Fri", "description": "dddd", "details": {
                "note": "nnnnn", "next_step": "ss", "capture_key": "k:1", "tags": ["a"]}}])["added"][0]["id"]
        overview = self.ok("entry", entry_id=eid)
        self.assertEqual({key: overview[key] for key in (
            "text_length", "due_length", "description_length", "note_length", "next_step_length",
            "capture_key_length", "group_path_length", "tag_count", "reference_count")}, {
            "text_length": 3, "due_length": 3, "description_length": 4, "note_length": 5,
            "next_step_length": 2, "capture_key_length": 3, "group_path_length": 4, "tag_count": 1,
            "reference_count": 0})


if __name__ == "__main__":
    import unittest
    unittest.main()
