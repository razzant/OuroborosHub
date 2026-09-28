"""smart_lists tests through the registered PluginAPI surfaces.

Tools are invoked the way the host dispatches them (``handler(ctx, **args)``
when the first parameter is a ctx slot) and routes the way the gateway does
(``await handler(request)``, a returned mapping becomes the JSON body). Every
test uses a fresh temporary state directory; persistence is checked by loading
a second, independent plugin module against the same directory.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import inspect
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is installed in Hub CI
    yaml = None

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
_COUNTER = itertools.count()

DECLARATIVE_TYPES = {
    "action", "audio", "chart", "code", "file", "form", "gallery", "image", "json", "kv", "key_value",
    "markdown", "poll", "progress", "status", "stream", "subscription", "tabs", "table", "video", "map",
    "calendar", "kanban", "group", "metric", "callout",
}


def load_plugin():
    """A fresh plugin module (fresh globals) loaded from the skill directory."""
    name = f"smart_lists_plugin_under_test_{next(_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def manifest_text() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def manifest_permissions() -> set:
    match = re.search(r"^permissions:\s*\[([^\]]*)\]", manifest_text(), re.MULTILINE)
    assert match, "SKILL.md must declare permissions inline"
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


def wants_ctx(handler) -> bool:
    """Mirror of the host rule: ctx is passed when the first parameter is a ctx slot."""
    params = list(inspect.signature(handler).parameters.values())
    if not params:
        return False
    first = params[0]
    if first.kind == first.VAR_POSITIONAL:
        return True
    return first.kind in (first.POSITIONAL_ONLY, first.POSITIONAL_OR_KEYWORD) and first.name in {
        "ctx", "context", "_ctx", "tool_context"}


class FakeRequest:
    def __init__(self, body=None, query=None, method="GET"):
        self._body = body
        self.query_params = dict(query or {})
        self.method = method

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakePluginAPI:
    """Records registrations and refuses anything the manifest did not declare."""

    def __init__(self, state_dir, permissions=None):
        self.state_dir = str(state_dir)
        self.permissions = set(manifest_permissions() if permissions is None else permissions)
        self.tools, self.routes, self.tabs, self.logs = {}, {}, {}, []

    def _require(self, permission):
        if permission not in self.permissions:
            raise AssertionError(f"undeclared permission {permission!r}")

    def get_state_dir(self):
        return self.state_dir

    def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
        self._require("tool")
        assert re.fullmatch(r"[A-Za-z0-9_]{1,24}", name), name
        assert name not in self.tools, f"duplicate tool {name}"
        self.tools[name] = {"handler": handler, "description": description, "schema": schema,
                            "timeout_sec": timeout_sec}

    def register_route(self, path, handler, *, methods=("GET",)):
        self._require("route")
        assert path not in self.routes, f"duplicate route {path}"
        self.routes[path] = {"handler": handler, "methods": tuple(methods)}

    def register_ui_tab(self, tab_id, title, *, icon="extension", render=None):
        self._require("widget")
        self.tabs[tab_id] = {"title": title, "icon": icon, "render": render}

    def log(self, level, message, *args, **kwargs):
        self.logs.append((level, message))

    def call_tool(self, tool_name, /, **args):
        handler = self.tools[tool_name]["handler"]
        raw = handler(None, **args) if wants_ctx(handler) else handler(**args)
        assert isinstance(raw, str)
        return json.loads(raw)

    def call_route(self, path, method="GET", body=None, query=None):
        route = self.routes[path]
        assert method in route["methods"], f"{method} not allowed on {path}"
        result = route["handler"](FakeRequest(body=body, query=query, method=method))
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
        assert isinstance(result, dict)
        json.dumps(result)  # the gateway serializes the mapping as JSON
        return result


def iter_components(components):
    for component in components:
        yield component
        yield from iter_components(component.get("components", []))
        for tab in component.get("tabs", []):
            yield from iter_components(tab.get("components", []))


class SkillCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = pathlib.Path(self._tmp.name) / "state"
        self.api = self.fresh_api()

    def fresh_api(self):
        api = FakePluginAPI(self.state_dir)
        load_plugin().register(api)
        return api

    def tool(self, tool_name, /, **args):
        return self.api.call_tool(tool_name, **args)

    def ok(self, tool_name, /, **args):
        result = self.tool(tool_name, **args)
        self.assertTrue(result["ok"], result)
        return result

    def refused(self, tool_name, code, /, **args):
        result = self.tool(tool_name, **args)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], code, result)
        return result

    def make_tree(self):
        home = self.ok("group", action="create", name="Home")["group"]["id"]
        groceries = self.ok("group", action="create", name="Groceries", parent="Home")["group"]["id"]
        dairy = self.ok("group", action="create", name="Dairy", parent="home / groceries")["group"]["id"]
        hardware = self.ok("group", action="create", name="Hardware", parent=home)["group"]["id"]
        work = self.ok("group", action="create", name="Work")["group"]["id"]
        return {"home": home, "groceries": groceries, "dairy": dairy, "hardware": hardware, "work": work}

    def add(self, group, *texts, request_id=None, **extra):
        result = self.ok("add", group=group, items=[{"text": text} for text in texts],
                         request_id=request_id or f"req-{next(_COUNTER)}", **extra)
        return [entry["id"] for entry in result["added"]]

    def store_bytes(self):
        return (self.state_dir / "store.json").read_bytes()


class RegistrationTests(SkillCase):
    def test_registers_declared_tools_routes_and_widget(self):
        self.assertEqual(set(self.api.tools), {"add", "read", "update", "complete", "move", "group", "select"})
        for name, tool in self.api.tools.items():
            self.assertTrue(wants_ctx(tool["handler"]), name)
            self.assertEqual(tool["schema"]["type"], "object", name)
            self.assertIs(tool["schema"].get("additionalProperties"), False, name)
            self.assertTrue(tool["description"].startswith("Smart Lists:"), name)
        self.assertEqual(self.api.tools["add"]["schema"]["required"], ["group", "items", "request_id"])
        self.assertEqual({path: route["methods"] for path, route in self.api.routes.items()}, {
            "view": ("GET",), "edit": ("POST",), "group": ("POST",), "select": ("GET",)})
        self.assertIn("even without", self.api.tools["add"]["description"])
        self.assertIn("even without saying 'add'", manifest_text())
        self.assertEqual(list(self.api.tabs), ["lists"])
        self.assertEqual(self.api.tabs["lists"]["render"]["kind"], "declarative")
        self.assertEqual(manifest_permissions(), {"tool", "route", "widget"})
        self.assertNotIn("env_from_settings: [OPENROUTER", manifest_text())

    def test_widget_uses_registered_routes_and_host_components(self):
        render = self.api.tabs["lists"]["render"]
        components = list(iter_components(render["components"]))
        self.assertLessEqual(len(components), 256)
        self.assertNotIn("add", {component.get("route") for component in components})
        for component in components:
            kind = component["type"]
            self.assertIn(kind, DECLARATIVE_TYPES)
            if kind in {"form", "action", "poll"}:
                route = self.api.routes[component["route"]]
                self.assertIn(component.get("method", "GET"), route["methods"])
            if kind == "form":
                self.assertTrue(all(field.get("name") for field in component["fields"]))
            if kind in {"table", "kv"}:
                rows = component["columns"] if kind == "table" else component["fields"]
                self.assertTrue(rows and all(row.get("path") for row in rows))
            if kind == "callout":
                self.assertIn(component.get("tone", "info"), {"info", "success", "warning", "danger"})
            if kind == "metric":
                self.assertTrue(component.get("label") and component.get("path"))

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_manifest_ui_tab_mirrors_registered_render(self):
        front = yaml.safe_load(manifest_text().split("---", 2)[1])
        self.assertEqual(front["name"], "smart_lists")
        self.assertEqual(front["ui_tab"]["tab_id"], "lists")
        self.assertEqual(front["ui_tab"]["render"], self.api.tabs["lists"]["render"])

    @unittest.skipUnless(os.environ.get("OUROBOROS_CORE_ROOT"), "set OUROBOROS_CORE_ROOT to use the core validator")
    def test_core_validator_accepts_both_widget_declarations(self):
        sys.path.insert(0, os.environ["OUROBOROS_CORE_ROOT"])
        try:
            from ouroboros.extension_ui_validation import validate_ui_render
        finally:
            sys.path.pop(0)
        validate_ui_render(copy.deepcopy(self.api.tabs["lists"]["render"]))
        if yaml is not None:
            front = yaml.safe_load(manifest_text().split("---", 2)[1])
            validate_ui_render(copy.deepcopy(front["ui_tab"]["render"]))

    def test_payload_imports_no_network_process_or_host_settings(self):
        allowed = {"__future__", "asyncio", "json", "pathlib", "typing", "importlib", "hashlib", "os", "re",
                   "secrets", "tempfile", "threading", "contextlib", "datetime", "fcntl"}
        for name in ("plugin.py", "lists_core.py"):
            tree = ast.parse((SKILL_DIR / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    imported.add(node.module.split(".")[0])
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"get_settings", "send_ws_message"}, name)
            self.assertLessEqual(imported, allowed, name)


class ToolFlowTests(SkillCase):
    def test_chat_capture_keeps_raw_text_and_persists_across_reload(self):
        tree = self.make_tree()
        raw = "  2 l Oat milk (the barista one)  "
        result = self.ok("add", group="Home / Groceries", request_id="r-1",
                         items=[{"text": raw}, {"text": "AA batteries x4"}])
        self.assertFalse(result["replayed"])
        self.assertEqual([entry["text"] for entry in result["added"]], [raw, "AA batteries x4"])
        self.assertEqual({entry["group_id"] for entry in result["added"]}, {tree["groceries"]})
        self.assertEqual(result["added"][0]["source"], "chat")

        on_disk = json.loads(self.store_bytes().decode("utf-8"))
        self.assertEqual(on_disk["schema_version"], 1)
        self.assertIn(raw, [entry["text"] for entry in on_disk["entries"].values()])

        reloaded = self.fresh_api()  # independent module, same state directory
        read = reloaded.call_tool("read", group="home/GROCERIES")
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["scope"], "Home / Groceries")
        self.assertEqual([entry["text"] for entry in read["entries"]], [raw, "AA batteries x4"])
        tree_paths = [row["path"] for row in reloaded.call_tool("read")["groups"]]
        self.assertEqual(tree_paths, ["Home", "Home / Groceries", "Home / Groceries / Dairy", "Home / Hardware",
                                      "Work"])

    def test_request_id_is_required_idempotent_and_conflict_checked(self):
        self.make_tree()
        self.refused("add", "invalid_input", group="Home", items=[{"text": "Soap"}])
        first = self.ok("add", group="Home", items=[{"text": "Soap"}, {"text": "Sponges"}], request_id="req-a")
        before = self.store_bytes()
        again = self.ok("add", group="Home", items=[{"text": "Soap"}, {"text": "Sponges"}], request_id="req-a")
        self.assertTrue(again["replayed"])
        self.assertEqual([e["id"] for e in again["added"]], [e["id"] for e in first["added"]])
        self.assertEqual(self.store_bytes(), before, "a replay must not write")
        self.refused("add", "request_conflict", group="Home", items=[{"text": "Towels"}], request_id="req-a")
        self.assertEqual(self.ok("read", group="Home", subtree=False)["total"], 2)

        respelled = self.ok("add", group=" home/GROCERIES ", items=[{"text": "Rice"}], request_id="req-c")
        retry = self.ok("add", group="Home / Groceries", items=[{"text": "Rice"}], request_id="req-c")
        self.assertTrue(retry["replayed"], "the same path spelled differently is still the same request")
        self.assertEqual(retry["added"], respelled["added"])

        entry_id = first["added"][0]["id"]
        self.ok("complete", entry_ids=[entry_id], request_id="req-b")
        replay = self.ok("complete", entry_ids=[entry_id], request_id="req-b")
        self.assertTrue(replay["replayed"])
        self.refused("complete", "request_conflict", entry_ids=[entry_id], done=False, request_id="req-b")

    def test_journal_does_not_retain_private_text_or_due_after_edit(self):
        self.make_tree()
        old_text, old_due = "private first wording", "private due wording"
        first = self.ok("add", group="Home", items=[{"text": old_text, "due": old_due}], request_id="private-add")
        entry_id = first["added"][0]["id"]
        self.assertNotIn(old_text, json.dumps(json.loads(self.store_bytes())["requests"]))
        self.assertNotIn(old_due, json.dumps(json.loads(self.store_bytes())["requests"]))

        self.ok("update", entry_id=entry_id, text="revised wording", clear_due=True, request_id="private-edit")
        journal = json.loads(self.store_bytes())["requests"]
        self.assertNotIn(old_text, json.dumps(journal))
        self.assertNotIn(old_due, json.dumps(journal))
        self.assertNotIn("revised wording", json.dumps(journal))
        replay = self.ok("add", group="Home", items=[{"text": old_text, "due": old_due}], request_id="private-add")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["added"][0]["id"], entry_id)
        self.assertEqual(replay["added"][0]["text"], "revised wording")
        self.assertIsNone(replay["added"][0]["due"])
        self.assertEqual(self.ok("read", group="Home")["total"], 1)

    def test_edit_sanitizes_legacy_journal_results(self):
        self.make_tree()
        entry_id = self.add("Home", "old secret")[0]
        path = self.state_dir / "store.json"
        doc = json.loads(path.read_text())
        record = next(iter(doc["requests"].values()))
        record["result"]["added"][0]["text"] = "old secret"
        path.write_text(json.dumps(doc))
        self.ok("update", entry_id=entry_id, text="new secret")
        self.assertNotIn("old secret", json.dumps(json.loads(self.store_bytes())["requests"]))

    def test_group_rename_does_not_retain_old_private_label_in_journal(self):
        first = self.ok("group", action="create", name="Old private label", request_id="group-first")
        group_id = first["group"]["id"]
        self.ok("group", action="rename", group=group_id, name="New label", request_id="group-rename")
        journal = json.loads(self.store_bytes())["requests"]
        self.assertNotIn("Old private label", json.dumps(journal))
        replay = self.ok("group", action="create", name="Old private label", request_id="group-first")
        self.assertTrue(replay["replayed"])
        self.assertEqual((replay["group"]["id"], replay["group"]["path"]), (group_id, "New label"))

    def test_repeated_text_is_kept_and_reported_not_merged(self):
        self.make_tree()
        first = self.add("Home / Groceries", "Oat milk")[0]
        second = self.ok("add", group="Home / Groceries", items=[{"text": "oat  MILK"}], request_id="dup-2")
        self.assertEqual(second["possible_duplicates"],
                         [{"entry_id": second["added"][0]["id"], "same_text_open_entries": [first]}])
        texts = [entry["text"] for entry in self.ok("read", group="Home / Groceries")["entries"]]
        self.assertEqual(texts, ["Oat milk", "oat  MILK"])
        self.add("Work", "Oat milk")  # other group: not reported, still stored
        self.assertEqual(self.ok("read", status="all")["total"], 3)

    def test_due_is_an_instant_only_with_explicit_offset(self):
        self.make_tree()
        values = ["2026-10-01T18:00+03:00", "2026-10-01T15:00:00Z", " tomorrow evening ", "2026-10-01",
                  "2026-10-01T18:00", "2026-13-01T18:00+03:00"]
        added = self.ok("add", group="Work", request_id="due-1",
                        items=[{"text": f"task {i}", "due": value} for i, value in enumerate(values)])["added"]
        dues = [entry["due"] for entry in added]
        self.assertEqual(dues[0], {"raw": "2026-10-01T18:00+03:00", "at": "2026-10-01T18:00:00+03:00"})
        self.assertEqual(dues[1], {"raw": "2026-10-01T15:00:00Z", "at": "2026-10-01T15:00:00+00:00"})
        for due, value in zip(dues[2:], values[2:]):
            self.assertEqual(due, {"raw": value}, "anything without an explicit offset stays raw only")
        entry_id = added[0]["id"]
        self.assertIsNone(self.ok("update", entry_id=entry_id, clear_due=True)["entry"]["due"])
        self.refused("update", "invalid_input", entry_id=entry_id, due="Friday", clear_due=True)
        self.refused("update", "invalid_input", entry_id=entry_id, clear_due="false")
        module = load_plugin()
        with self.assertRaisesRegex(module.lists_core.ListError, "clear_due must be true or false"):
            module.lists_core.SmartLists(self.state_dir).edit(entry_id, clear_due="false")

    def test_update_complete_reopen_and_move(self):
        tree = self.make_tree()
        milk, bulbs = self.add("Home / Groceries", "milk", "light bulbs")
        updated = self.ok("update", entry_id=milk, text=" Milk, 2 bottles ")
        self.assertEqual(updated["entry"]["text"], " Milk, 2 bottles ")
        self.assertEqual(updated["changed"], ["text"])
        self.refused("update", "invalid_input", entry_id=milk)
        self.refused("update", "invalid_input", entry_id=milk, text="   ")
        self.refused("update", "not_found", entry_id="e_0000000000", text="x")

        done = self.ok("complete", entry_ids=[milk])
        self.assertEqual(done["changed"][0]["status"], "done")
        self.assertTrue(done["changed"][0]["completed_at"])
        self.assertEqual(self.ok("complete", entry_ids=[milk])["unchanged"], [milk])
        self.assertEqual([e["id"] for e in self.ok("read", group="Home", status="done")["entries"]], [milk])
        reopened = self.ok("complete", entry_ids=[milk], done=False)
        self.assertEqual(reopened["changed"][0]["status"], "open")
        self.assertIsNone(reopened["changed"][0]["completed_at"])

        moved = self.ok("move", entry_ids=[bulbs], to_group="Home / Hardware")
        self.assertEqual(moved["moved"][0]["group_id"], tree["hardware"])
        self.assertEqual(self.ok("move", entry_ids=[bulbs], to_group=tree["hardware"])["unchanged"], [bulbs])
        self.refused("move", "not_found", entry_ids=[bulbs], to_group="Garden")
        only_groceries = self.ok("read", group="Home / Groceries", subtree=False)["entries"]
        self.assertEqual([e["id"] for e in only_groceries], [milk])

    def test_subtree_selection_is_read_only_and_open_only(self):
        self.make_tree()
        self.add("Home", "doormat")
        milk, butter = self.add("Home / Groceries", "milk", "butter")
        self.add("Home / Groceries / Dairy", "yogurt")
        self.add("Home / Hardware", "hinges")
        self.add("Work", "printer paper")
        self.ok("complete", entry_ids=[butter])
        before = self.store_bytes()
        selection = self.ok("select", group="home")
        self.assertTrue(selection["read_only"])
        self.assertEqual([(row["text"], row["group_path"]) for row in selection["entries"]], [
            ("doormat", "Home"),
            ("milk", "Home / Groceries"),
            ("yogurt", "Home / Groceries / Dairy"),
            ("hinges", "Home / Hardware"),
        ])
        self.assertEqual(selection["count"], 4)
        self.assertEqual((selection["total"], selection["truncated"]), (4, False))
        self.assertEqual(selection["groups_included"],
                         ["Home", "Home / Groceries", "Home / Groceries / Dairy", "Home / Hardware"])
        self.assertEqual(self.store_bytes(), before, "select must not write")
        self.assertEqual([row["text"] for row in self.ok("select", group="Home/Groceries")["entries"]],
                         ["milk", "yogurt"])
        self.refused("select", "not_found", group="Garden")

    def test_subtree_selection_caps_rows_and_reports_total(self):
        self.ok("group", action="create", name="Home")
        for batch in range(6):
            self.ok("add", group="Home", request_id=f"selection-{batch}",
                    items=[{"text": f"item {batch * 100 + index}"} for index in range(100)])
        before = self.store_bytes()
        selection = self.ok("select", group="Home")
        self.assertEqual((selection["count"], selection["total"], selection["truncated"]), (500, 600, True))
        self.assertEqual((len(selection["entries"]), selection["entries"][-1]["text"]), (500, "item 499"))
        widget = self.api.call_route("select", query={"group": "Home"})
        self.assertEqual((widget["count"], widget["total"], widget["truncated"]), (500, 600, True))
        self.assertEqual(len(widget["rows"]), 500)
        self.assertIn("500 of 600", widget["warning"])
        self.assertEqual(self.store_bytes(), before)

    def test_group_tree_edits_keep_one_parent_and_no_cycles(self):
        tree = self.make_tree()
        self.refused("group", "conflict", action="create", name=" groceries ", parent="Home")
        self.refused("group", "invalid_input", action="create", name="Food/Drinks")
        self.refused("group", "conflict", action="move", group="Home", parent="Home / Groceries / Dairy")
        self.refused("group", "conflict", action="move", group="Home", parent="Home")

        moved = self.ok("group", action="move", group="Home / Groceries / Dairy", parent="")["group"]
        self.assertEqual((moved["path"], moved["parent_id"]), ("Dairy", None))
        renamed = self.ok("group", action="rename", group="dairy", name="Dairy aisle")["group"]
        self.assertEqual(renamed["id"], tree["dairy"])
        self.ok("group", action="move", group=tree["dairy"], parent=tree["groceries"])
        self.assertEqual(self.ok("read", group=tree["dairy"])["scope"], "Home / Groceries / Dairy aisle")

        self.add("Work", "stapler")
        self.refused("group", "conflict", action="delete", group="Home")  # has sub-groups
        self.refused("group", "conflict", action="delete", group="Work")  # holds an entry
        deleted = self.ok("group", action="delete", group="Home / Hardware")
        self.assertEqual(deleted["deleted"]["id"], tree["hardware"])
        missing = self.refused("read", "not_found", group="Home / Hardware")
        self.assertIn("'Home / Groceries'", missing["error"]["message"])

    def test_unknown_or_malformed_arguments_are_refused(self):
        self.make_tree()
        self.refused("add", "invalid_input", group="Home", items=[{"text": "bad\ud800"}], request_id="unicode")
        self.refused("group", "invalid_input", action="create", name="bad\ud800")
        self.refused("add", "invalid_input", group="Home", items=[{"text": "x"}], request_id="r", quantity=2)
        self.refused("add", "invalid_input", group="Home", items=[{"text": "x", "qty": 2}], request_id="r")
        self.refused("add", "invalid_input", group="Home", items=[], request_id="r")
        self.refused("add", "invalid_input", group="Home", items=[{"text": "x"}], request_id="bad id!")
        self.refused("read", "invalid_input", limit=0)
        self.refused("complete", "invalid_input", entry_ids=["not-an-id"])
        self.refused("group", "invalid_input", action="archive", group="Home")
        self.assertEqual(self.ok("read", status="all")["total"], 0)


class RouteTests(SkillCase):
    def test_widget_routes_share_the_store_with_tools(self):
        view = self.api.call_route("view")
        self.assertTrue(view["ok"])
        self.assertTrue(view["empty_hint"])
        self.assertEqual(view["stats"], {"groups": 0, "open": 0, "done": 0})

        created = self.api.call_route("group", "POST", {"action": "create", "name": "Trip", "group": "",
                                                        "parent": ""})
        self.assertTrue(created["ok"], created)
        self.assertIn("Trip", created["notice"])
        self.api.call_route("group", "POST", {"action": "create", "name": "Packing", "parent": "Trip"})
        added = self.ok("add", group="Trip / Packing", request_id="capture-1",
                        items=[{"text": "Sunscreen SPF 50", "due": "2026-10-03T07:30+02:00"}])
        entry_id = added["added"][0]["id"]

        read = self.ok("read", group="Trip")
        self.assertEqual(read["entries"][0]["source"], "chat")
        self.assertEqual(read["entries"][0]["due"]["at"], "2026-10-03T07:30:00+02:00")

        tool_added = self.ok("add", group="Trip", request_id="chat-1", items=[{"text": "Passport"}])
        refreshed = self.api.call_route("view")
        self.assertEqual({row["item"] for row in refreshed["open_rows"]}, {"Sunscreen SPF 50", "Passport"})
        self.assertEqual([row["group"] for row in refreshed["tree_rows"]], ["Trip", "Trip / Packing"])

        edited = self.api.call_route("edit", "POST", {"entry_id": entry_id, "status": "done", "move_to": "Trip",
                                                      "text": "Sunscreen", "due": "", "clear_due": True})
        self.assertTrue(edited["ok"], edited)
        self.assertEqual(edited["done_rows"][0]["item"], "Sunscreen")
        self.assertEqual(edited["done_rows"][0]["group"], "Trip")
        self.assertEqual(edited["done_rows"][0]["due"], "")
        self.assertEqual(edited["stats"], {"groups": 2, "open": 1, "done": 1})

        selection = self.api.call_route("select", query={"group": "trip"})
        self.assertTrue(selection["ok"])
        self.assertEqual([row["id"] for row in selection["rows"]], [tool_added["added"][0]["id"]])

        after_reload = self.fresh_api().call_route("view")
        self.assertEqual(after_reload["stats"], edited["stats"])

    def test_delete_group_retry_uses_id_without_retaining_old_private_name(self):
        created = self.api.call_route("group", "POST", {"action": "create", "name": "Private group"})
        group_id = created["tree_rows"][0]["id"]
        create_body = {"action": "create", "name": "Another group", "request_id": "create-one"}
        another = self.api.call_route("group", "POST", create_body)
        self.assertTrue(another["ok"], another)
        another_id = next(row["id"] for row in another["tree_rows"] if row["group"] == "Another group")
        self.assertTrue(self.api.call_route("group", "POST", {"action": "delete", "group": another_id})["ok"])
        created_replay = self.api.call_route("group", "POST", create_body)
        self.assertTrue(created_replay["ok"], created_replay)
        self.assertIn(another_id, created_replay["notice"])
        body = {"action": "delete", "group": group_id, "request_id": "delete-one"}
        first = self.api.call_route("group", "POST", body)
        self.assertTrue(first["ok"], first)
        replay = self.api.call_route("group", "POST", body)
        self.assertTrue(replay["ok"], replay)
        self.assertIn(group_id, replay["notice"])
        self.assertNotIn("Private group", json.dumps(json.loads(self.store_bytes())["requests"]))

    def test_widget_refusals_keep_the_view_and_never_set_error(self):
        self.make_tree()
        self.add("Work", "stapler")
        for path, body in (
            ("edit", {"entry_id": "e_0000000000", "status": "done"}),
            ("edit", {"entry_id": "", "status": "done"}),
            ("group", {"action": "delete", "group": "Work"}),
        ):
            with self.subTest(path=path, body=body):
                result = self.api.call_route(path, "POST", body)
                self.assertFalse(result["ok"])
                self.assertTrue(result["warning"])
                self.assertNotIn("error", result)
                self.assertEqual([row["item"] for row in result["open_rows"]], ["stapler"])
        bad_body = self.api.call_route("edit", "POST", ValueError("not json"))
        self.assertFalse(bad_body["ok"])
        missing = self.api.call_route("select", query={"group": "Garden"})
        self.assertEqual((missing["ok"], missing["rows"]), (False, []))
        self.assertNotIn("error", missing)


class StoreSafetyTests(SkillCase):
    def test_unreadable_store_is_reported_and_left_untouched(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for content in (b"{not json", json.dumps({"schema_version": 99, "next_seq": 1, "groups": {},
                                                  "entries": {}, "requests": {}}).encode()):
            with self.subTest(content=content[:20]):
                (self.state_dir / "store.json").write_bytes(content)
                self.refused("add", "store_unreadable", group="Home", items=[{"text": "x"}], request_id="r")
                self.refused("read", "store_unreadable")
                view = self.api.call_route("view")
                self.assertFalse(view["ok"])
                self.assertIn("left untouched", view["warning"])
                self.assertEqual(self.store_bytes(), content)

    def test_malformed_parent_id_type_is_store_unreadable(self):
        group_id = self.ok("group", action="create", name="Home")["group"]["id"]
        path = self.state_dir / "store.json"
        good = json.loads(path.read_text())
        for bad_parent in ([], {}, 42, False):
            with self.subTest(parent=bad_parent):
                doc = copy.deepcopy(good)
                doc["groups"][group_id]["parent_id"] = bad_parent
                content = json.dumps(doc).encode()
                path.write_bytes(content)
                self.refused("read", "store_unreadable")
                self.refused("add", "store_unreadable", group="Home", items=[{"text": "x"}], request_id="r")
                self.assertEqual(self.store_bytes(), content)

    def test_malformed_entry_group_id_type_is_store_unreadable(self):
        self.ok("group", action="create", name="Home")
        entry_id = self.add("Home", "bread")[0]
        path = self.state_dir / "store.json"
        good = json.loads(path.read_text())
        for bad_group in ([], {}, 42, False):
            with self.subTest(group=bad_group):
                doc = copy.deepcopy(good)
                doc["entries"][entry_id]["group_id"] = bad_group
                content = json.dumps(doc).encode()
                path.write_bytes(content)
                self.refused("read", "store_unreadable")
                self.assertEqual(self.store_bytes(), content)

    def test_filesystem_failure_is_a_typed_refusal(self):
        blocker = pathlib.Path(self._tmp.name) / "blocker"
        blocker.write_text("a regular file where the state directory should be", encoding="utf-8")
        api = FakePluginAPI(blocker / "state")
        load_plugin().register(api)
        result = api.call_tool("add", group="Home", items=[{"text": "x"}], request_id="r")
        self.assertEqual((result["ok"], result["error"]["code"]), (False, "store_io"))
        view = api.call_route("view")
        self.assertFalse(view["ok"])
        self.assertNotIn("error", view)

    def test_concurrent_thread_writers_do_not_lose_entries(self):
        self.ok("group", action="create", name="Inbox")
        errors = []

        def worker(index):
            for step in range(10):
                result = self.tool("add", group="Inbox", items=[{"text": f"t{index}-{step}"}],
                                   request_id=f"t{index}-{step}")
                if not result["ok"]:
                    errors.append(result)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.ok("read", limit=500)["total"], 80)

    @unittest.skipIf(os.name == "nt", "advisory file lock is POSIX-only")
    def test_concurrent_process_writers_do_not_lose_entries(self):
        self.ok("group", action="create", name="Inbox")
        script = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('core', sys.argv[1])\n"
            "core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)\n"
            "lists = core.SmartLists(sys.argv[2])\n"
            "for step in range(15):\n"
            "    lists.add('Inbox', [{'text': sys.argv[3] + str(step)}], request_id=sys.argv[3] + str(step))\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", script, str(SKILL_DIR / "lists_core.py"),
                                   str(self.state_dir), f"p{index}-"]) for index in range(4)]
        self.assertEqual([proc.wait(timeout=60) for proc in procs], [0, 0, 0, 0])
        self.assertEqual(self.ok("read", limit=500)["total"], 60)


if __name__ == "__main__":
    unittest.main()
