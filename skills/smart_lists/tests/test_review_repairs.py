"""0.6.3 repairs: expected due shapes, HTTP(S) scheme case, packed agent pages and the card draft slot."""
import json
import threading
import time
from unittest import mock

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


def snapshot(**changes):
    """A card form as the widget keeps it (invented content)."""
    value = {"v": 1, "entry_id": None, "title": "Новая задача", "base": None,
             "form": {"text": "Новая задача", "description": "Описание в работе", "stage": "todo",
                      "turn": "unassigned", "due": "", "tags": []},
             "ops": [], "conflicts": {}, "refs_moved": False, "unread": False, "pending": None, "creating": None,
             "typed": {"tag": "", "url": "", "label": ""}}
    value.update(changes)
    return value


class WidgetDraftTests(SkillCase):
    """The card editor's draft slot sits beside the store, never in it, and is never overwritten blindly."""

    def draft(self):
        return self.api.call_route("draft", "GET")

    def write(self, expected, draft, write_id="w1", **extra):
        return self.api.call_route("draft", "POST", {"expected_revision": expected, "write_id": write_id,
                                                     "draft": draft, **extra})

    def store_files(self):
        return {path.name: path.read_bytes() for path in self.state_dir.iterdir()
                if path.is_file() and not path.name.startswith("widget_draft")}

    def test_a_draft_round_trips_beside_the_store_without_touching_it(self):
        self.ok("group", action="create", name="Inbox")
        before = self.store_files()
        empty = self.draft()
        self.assertEqual((empty["ok"], empty["revision"], empty["draft"]), (True, "", None))
        first = self.write("", snapshot())
        self.assertTrue(first["ok"], first)
        read = self.draft()
        self.assertEqual((read["revision"], read["write_id"], read["draft"]), (first["revision"], "w1", snapshot()))
        # A half-typed field longer than the store accepts is kept as typed, not cut.
        long = snapshot(form={**snapshot()["form"], "description": "я" * 20000})
        second = self.write(first["revision"], long, write_id="w2")
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.draft()["draft"]["form"]["description"], "я" * 20000)
        # Both link steps the widget writes, with a label longer than a stored one may be, are kept as written.
        steps = [{"drop": True, "target": "https://example.org/a", "label": "Было"},
                 {"ref": {"label": "п" * 300, "target": "https://example.org/a"}}]
        second = self.write(second["revision"], snapshot(ops=steps), write_id="w2b")
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.draft()["draft"]["ops"], steps)
        cleared = self.write(second["revision"], None, write_id="w3")
        self.assertTrue(cleared["ok"], cleared)
        self.assertEqual((self.draft()["revision"], self.draft()["draft"]), (cleared["revision"], None))
        # The store, its sentinel and lock are byte-identical; tools and exports never see the draft.
        self.assertEqual(self.store_files(), before)
        self.assertNotIn("Описание в работе", json.dumps(self.ok("read", group="Inbox"), ensure_ascii=False))
        self.write(cleared["revision"], snapshot(), write_id="w4")
        export = self.module._service().export_document()
        self.assertNotIn("Описание в работе", json.dumps(export, ensure_ascii=False))

    def test_a_write_based_on_an_older_revision_is_refused_with_what_the_slot_holds(self):
        first = self.write("", snapshot(title="Первое окно"))
        # A second window that read the slot empty cannot replace the first window's draft.
        refused = self.write("", snapshot(title="Второе окно"), write_id="w2")
        self.assertEqual((refused["ok"], refused["code"]), (False, "draft_conflict"))
        self.assertEqual((refused["current"]["revision"], refused["current"]["write_id"]), (first["revision"], "w1"))
        self.assertEqual(refused["current"]["draft"]["title"], "Первое окно")
        cleared = self.write(first["revision"], None, write_id="w3")
        # A late write made before the clear cannot bring the draft back.
        late = self.write(first["revision"], snapshot(title="Поздняя запись"), write_id="w4")
        self.assertEqual(late["code"], "draft_conflict")
        self.assertIsNone(late["current"]["draft"])
        self.assertEqual((self.draft()["revision"], self.draft()["draft"]), (cleared["revision"], None))

    def test_refused_writes_keep_the_previous_draft_and_say_why(self):
        kept = self.write("", snapshot())
        path = self.state_dir / "widget_draft.json"
        raw = path.read_bytes()
        form = snapshot()["form"]
        for value in ({**snapshot(), "extra": 1}, {**snapshot(), "v": 2}, "text", [],
                      snapshot(form={**form, "tags": ["ok", 3]}), snapshot(form={**form, "due": None}),
                      snapshot(typed={"tag": ""}), snapshot(pending={"keys": [], "sent": {}}),
                      snapshot(title="\ud800"), snapshot(ops=[{}]), snapshot(ops=["drop"]),
                      snapshot(ops=[{"drop": True, "target": "https://example.org/a"}]),
                      snapshot(ops=[{"drop": False, "target": "https://example.org/a", "label": "A"}]),
                      snapshot(ops=[{"ref": {"target": "https://example.org/a"}}]),
                      snapshot(ops=[{"ref": {"label": 1, "target": "https://example.org/a"}}]),
                      snapshot(ops=[{"ref": {"label": "A", "target": "https://example.org/a"}, "drop": True}])):
            answer = self.write(kept["revision"], value, write_id="bad")
            self.assertEqual((answer["ok"], answer["code"]), (False, "invalid_input"), value)
        for expected, write_id in ((None, "w"), (5, "w"), (kept["revision"], ""), (kept["revision"], "x" * 65)):
            answer = self.api.call_route("draft", "POST", {"expected_revision": expected, "write_id": write_id,
                                                           "draft": None})
            self.assertEqual(answer["code"], "invalid_input", (expected, write_id))
        with mock.patch.object(self.module.lists_core, "MAX_DRAFT_BYTES", 2000):
            answer = self.write(kept["revision"], snapshot(form={**form, "description": "x" * 3000}), write_id="big")
        self.assertEqual(answer["code"], "draft_too_large")
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.draft()["revision"], kept["revision"])

    def test_the_store_lock_never_holds_the_draft_and_a_busy_draft_changes_nothing(self):
        service = self.module._service()

        def holding(lock_of):
            held, release = threading.Event(), threading.Event()

            def hold():
                with lock_of():
                    held.set()
                    release.wait(10)
            thread = threading.Thread(target=hold)
            thread.start()
            self.assertTrue(held.wait(5))
            return lambda: (release.set(), thread.join(5))

        done = holding(service.store._locked)  # the main store is busy, for example with an export
        try:
            started = time.monotonic()
            answer = self.write("", snapshot())
            self.assertTrue(answer["ok"], answer)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            done()
        raw = service.draft.path.read_bytes()
        with mock.patch.object(self.module.lists_core, "DRAFT_LOCK_TIMEOUT_SEC", 0.1):
            done = holding(service.draft._locked)
            try:
                busy = self.write(answer["revision"], None, write_id="w2")
            finally:
                done()
        self.assertEqual((busy["ok"], busy["code"]), (False, "draft_busy"))
        self.assertEqual(service.draft.path.read_bytes(), raw)

    def test_an_unreadable_draft_is_reported_and_set_aside_only_when_asked(self):
        path = self.state_dir / "widget_draft.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not json")
        self.assertEqual(self.draft()["code"], "draft_unreadable")
        self.assertEqual(self.write("", snapshot())["code"], "draft_unreadable")
        self.assertEqual(path.read_bytes(), b"{not json")
        replaced = self.write("", None, write_id="w2", replace_unreadable=True)
        self.assertTrue(replaced["ok"], replaced)
        self.assertEqual((self.draft()["revision"], self.draft()["draft"]), (replaced["revision"], None))
        # A second damaged file is set aside too, and the first one's bytes are never overwritten.
        path.write_bytes(b"[also broken")
        again = self.write("", None, write_id="w3", replace_unreadable=True)
        self.assertTrue(again["ok"], again)
        aside = {item.name: item.read_bytes() for item in self.state_dir.glob("widget_draft.unreadable*")}
        self.assertEqual(sorted(aside.values()), [b"[also broken", b"{not json"])
        self.assertEqual(aside, {replaced["set_aside"]: b"{not json", again["set_aside"]: b"[also broken"})
        # An envelope without its draft, and a draft whose link step is not one the widget writes, are
        # unreadable too: reported as such and set aside on request, never a server error.
        for number, damaged in enumerate(({"revision": "r", "write_id": "w", "saved_at": "t"},
                                          {"revision": "r", "write_id": "w", "saved_at": "t",
                                           "draft": snapshot(ops=[{}])})):
            raw = json.dumps(damaged).encode()
            path.write_bytes(raw)
            self.assertEqual(self.draft()["code"], "draft_unreadable")
            self.assertEqual(self.write("r", None, write_id=f"x{number}")["code"], "draft_unreadable")
            self.assertEqual(path.read_bytes(), raw)
            set_aside = self.write("", None, write_id=f"y{number}", replace_unreadable=True)
            self.assertTrue(set_aside["ok"], set_aside)
            self.assertEqual((self.state_dir / set_aside["set_aside"]).read_bytes(), raw)
        # A slot cleared by the widget still holds `draft: null` and reads as empty.
        self.assertIsNone(self.draft()["draft"])


if __name__ == "__main__":
    import unittest
    unittest.main()
