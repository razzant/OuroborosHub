"""smart_lists tests through the registered PluginAPI surfaces.

Tools are invoked the way the host dispatches them (``handler(ctx, **args)``
when the first parameter is a ctx slot) and routes the way the gateway does
(``await handler(request)``, a returned mapping becomes the JSON body). Every
test uses a fresh temporary state directory, initialised through the ``store``
tool unless the test is about the uninitialised lifecycle; persistence is
checked by loading a second, independent plugin module against the same
directory.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import copy
import errno
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
import urllib.parse
import zlib
from unittest import mock

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
    INIT = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = pathlib.Path(self._tmp.name) / "state"
        self.api = self.fresh_api()
        if self.INIT:
            self.ok("store", action="init")

    def fresh_api(self, state_dir=None):
        api = FakePluginAPI(state_dir or self.state_dir)
        self.module = load_plugin()
        self.module.register(api)
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
        self.assertEqual(set(self.api.tools), {"add", "read", "update", "complete", "move", "group", "select",
                                               "delete", "store"})
        for name, tool in self.api.tools.items():
            self.assertTrue(wants_ctx(tool["handler"]), name)
            self.assertEqual(tool["schema"]["type"], "object", name)
            self.assertIs(tool["schema"].get("additionalProperties"), False, name)
            self.assertTrue(tool["description"].startswith("Smart Lists:"), name)
        self.assertEqual(self.api.tools["add"]["schema"]["required"], ["group", "items", "request_id"])
        self.assertEqual({path: route["methods"] for path, route in self.api.routes.items()}, {
            "view": ("GET",), "edit": ("POST",), "group": ("POST",), "select": ("GET",), "store": ("POST",),
            "export": ("GET",)})
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
            if kind in {"form", "action", "poll", "file"}:
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
        for route, target in (("edit", "edit_result"), ("group", "group_result"),
                              ("store", "store_result"), ("select", "selection")):
            form = next(c for c in components if c["type"] == "form" and c["route"] == route)
            self.assertEqual(form["target"], target)
            self.assertTrue(any(c["type"] == "callout" and c.get("target") == target
                                and c.get("path") == "error" and c.get("tone") == "danger"
                                for c in components))

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
        allowed = {"__future__", "asyncio", "copy", "json", "pathlib", "typing", "importlib", "hashlib", "os", "re",
                   "secrets", "tempfile", "threading", "contextlib", "datetime", "fcntl", "msvcrt", "errno",
                   "time", "math", "base64", "zlib"}
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
    def test_duplicate_report_is_not_retained_as_unbounded_replay_data(self):
        self.ok("group", action="create", name="Home")
        self.add("Home", "milk", request_id="first")
        result = self.ok("add", group="Home", items=[{"text": "milk"}], request_id="second")
        self.assertTrue(result["possible_duplicates"])
        journal = json.loads(self.store_bytes())["requests"]["second"]["result"]
        self.assertEqual(journal["possible_duplicates"], [])
        self.assertTrue(journal["duplicates_omitted_on_replay"])
        replay = self.ok("add", group="Home", items=[{"text": "milk"}], request_id="second")
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["duplicates_omitted_on_replay"])
        self.assertEqual(self.ok("read", group="Home")["total"], 2)

    def test_sentinel_write_failure_never_commits_unprotected_store(self):
        before = self.store_bytes()
        store = self.module._service().store
        with mock.patch.object(store, "_write_meta", side_effect=OSError("disk full")):
            self.refused("group", "store_io", action="create", name="Later")
        self.assertEqual(self.store_bytes(), before)
        self.assertEqual(self.ok("read")["groups"], [])

    def test_retired_request_ids_stay_refused_without_a_history_cap(self):
        self.ok("group", action="create", name="Home")
        core = self.module.lists_core
        with mock.patch.object(core, "MAX_REQUESTS", 2):
            for index in range(30):
                self.ok("add", group="Home", items=[{"text": f"item {index}"}], request_id=f"id-{index}")
            before = self.store_bytes()
            for index in range(28):
                for text in (f"item {index}", "changed wording"):
                    self.refused("add", "request_expired", group="Home", items=[{"text": text}],
                                 request_id=f"id-{index}")
            self.assertEqual(self.store_bytes(), before, "a refused retry writes nothing")
            self.assertTrue(self.ok("add", group="Home", items=[{"text": "item 29"}], request_id="id-29")["replayed"])
            self.assertFalse(self.ok("add", group="Home", items=[{"text": "new"}], request_id="id-new")["replayed"])
            doc = json.loads(self.store_bytes())
            self.assertNotIn("expired_requests", doc)
            self.assertEqual(len(doc["requests"]), 2)
            self.assertEqual((doc["retired_requests"]["bits"], doc["retired_requests"]["hashes"]),
                             (core.BLOOM_BITS, core.BLOOM_HASHES))
            replay = self.ok("store", action="status")["replay"]
            self.assertEqual(replay["recent_ids"], 2)
            self.assertAlmostEqual(replay["retired_ids_estimate"], 29, delta=1)
            self.assertLess(replay["false_refusal_rate"], 1e-12)
        self.assertEqual(self.ok("read", group="Home", limit=500)["total"], 31)

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
        self.assertEqual(on_disk["schema_version"], 3)
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

    def test_replay_description_handles_a_group_deleted_after_add(self):
        group = self.ok("group", action="create", name="Temporary")["group"]["id"]
        entry = self.add(group, "temporary item", request_id="gone-add")[0]
        self.ok("delete", entry_ids=[entry])
        self.ok("delete", entry_ids=[entry], action="erase")
        self.ok("group", action="delete", group=group)
        replay = self.ok("add", group=group, items=[{"text": "temporary item"}], request_id="gone-add")
        self.assertEqual(replay["group"], {"id": group, "gone": True})
        self.assertIn(group, self.module._describe(replay))

    def test_repeated_text_is_kept_and_reported_not_merged(self):
        self.make_tree()
        first = self.add("Home / Groceries", "Oat milk")[0]
        second = self.ok("add", group="Home / Groceries", items=[{"text": "oat  MILK"}], request_id="dup-2")
        self.assertEqual(second["possible_duplicates"],
                         [{"entry_id": second["added"][0]["id"], "same_text_open_entries": [first], "total": 1,
                           "truncated": False}])
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
        self.assertEqual(selection["total"], 600)
        self.assertTrue(selection["truncated"])
        self.assertLess(len(selection["entries"]), 500)
        self.assertEqual(selection["count"], len(selection["entries"]))
        self.assertEqual(selection["result_counts"]["entries"],
                         {"total": 500, "shown": selection["count"], "truncated": True})
        self.assertEqual(selection["next_offset"], selection["count"])
        later = self.ok("select", group="Home", offset=selection["next_offset"])
        self.assertEqual(later["entries"][0]["text"], f"item {selection['count']}")
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
        self.assertEqual(view["stats"], {"groups": 0, "open": 0, "done": 0, "deleted": 0})

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
        self.assertEqual(edited["stats"], {"groups": 2, "open": 1, "done": 1, "deleted": 0})

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

    def test_widget_refusals_keep_the_view_and_show_form_error(self):
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
                self.assertEqual(result["error"], result["warning"])
                self.assertEqual([row["item"] for row in result["open_rows"]], ["stapler"])
                self.assertEqual([row["item"] for row in self.api.call_route("view")["open_rows"]], ["stapler"])
        bad_body = self.api.call_route("edit", "POST", ValueError("not json"))
        self.assertFalse(bad_body["ok"])
        missing = self.api.call_route("select", query={"group": "Garden"})
        self.assertEqual((missing["ok"], missing["rows"]), (False, []))
        self.assertEqual(missing["error"], missing["warning"])


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


def assert_same_lists(case, left, right):
    """Groups, entries (every field, trash included) and the request journal are equal."""
    for key in ("groups", "entries", "requests", "next_seq", "store_id"):
        case.assertEqual(left[key], right[key], key)


class LifecycleTests(SkillCase):
    INIT = False

    def export_file(self):
        result = self.ok("store", action="export")
        path = pathlib.Path(result["file"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.state_dir / "backups")
        return result, path

    def build_lists(self):
        self.ok("store", action="init")
        self.ok("group", action="create", name="Home")
        self.ok("group", action="create", name="Groceries", parent="Home")
        self.ok("group", action="create", name="Work")
        milk, bread, soap = [entry["id"] for entry in self.ok(
            "add", group="Home / Groceries", request_id="capture-1",
            items=[{"text": "  2 l oat milk ", "due": "2026-10-01T18:00+03:00"}, {"text": "bread"},
                   {"text": "soap", "due": "tomorrow"}])["added"]]
        self.ok("complete", entry_ids=[bread], request_id="done-1")
        self.ok("delete", entry_ids=[soap], request_id="trash-1")
        self.ok("add", group="Work", request_id="capture-2", items=[{"text": "renew badge"}])
        return {"milk": milk, "bread": bread, "soap": soap}

    def test_first_run_is_explicit_and_never_shows_an_empty_list(self):
        for tool, args in (("read", {}), ("select", {"group": "Home"}),
                           ("add", {"group": "Home", "items": [{"text": "x"}], "request_id": "r1"}),
                           ("group", {"action": "create", "name": "Home"})):
            with self.subTest(tool=tool):
                result = self.refused(tool, "store_not_initialized", **args)
                self.assertIn("restore", result["error"]["message"])
        self.assertFalse((self.state_dir / "store.json").exists(), "a refusal must not create the store")
        status = self.ok("store", action="status")
        self.assertEqual((status["state"], status["counts"]), ("uninitialized", None))
        view = self.api.call_route("view")
        self.assertFalse(view["ok"])
        self.assertEqual(view["store"]["state"], "uninitialized")
        self.assertFalse(view["store"]["exportable"])
        self.assertIn("Store tab", view["warning"])
        self.assertNotIn("error", view)

        created = self.ok("store", action="init")
        self.assertEqual((created["generation"], created["backup"], created["replaced"]), (1, None, None))
        self.assertEqual(self.ok("read")["total"], 0)
        self.refused("store", "store_exists", action="init")
        self.assertEqual(self.ok("store", action="status")["state"], "ready")
        meta = json.loads((self.state_dir / "store_meta.json").read_text())
        self.assertEqual((meta["store_id"], meta["generation"]), (created["store_id"], 1))

    def test_missing_store_after_writes_is_a_typed_refusal_until_restored(self):
        ids = self.build_lists()
        before = json.loads((self.state_dir / "store.json").read_text())
        _export, path = self.export_file()
        (self.state_dir / "store.json").unlink()

        for tool, args in (("read", {}), ("add", {"group": "Home", "items": [{"text": "x"}], "request_id": "r9"}),
                           ("complete", {"entry_ids": [ids["milk"]]})):
            with self.subTest(tool=tool):
                message = self.refused(tool, "store_missing", **args)["error"]["message"]
                self.assertIn("3 groups, 4 entries", message)
        self.assertFalse((self.state_dir / "store.json").exists(), "no empty store may replace the missing one")
        self.assertEqual(self.ok("store", action="status")["state"], "missing")
        view = self.api.call_route("view")
        self.assertEqual((view["ok"], view["store"]["state"], view["open_rows"]), (False, "missing", []))
        self.refused("store", "store_missing", action="init")

        restored = self.ok("store", action="restore", file=path.name)
        self.assertEqual(restored["backup"], None)
        self.assertIn("missing store file", restored["replaced"])
        after = json.loads((self.state_dir / "store.json").read_text())
        assert_same_lists(self, before, after)
        self.assertGreater(after["generation"], before["generation"])
        self.assertEqual([e["text"] for e in self.ok("read", status="all")["entries"]],
                         ["  2 l oat milk ", "bread", "renew badge"])

    def test_start_over_after_a_missing_store_needs_replace(self):
        self.build_lists()
        (self.state_dir / "store.json").unlink()
        fresh = self.ok("store", action="init", replace=True)
        self.assertEqual(fresh["counts"]["entries"], 0)
        self.assertEqual(self.ok("read", status="all")["total"], 0)

    def test_older_or_foreign_store_file_is_refused_and_left_untouched(self):
        self.build_lists()
        older = self.store_bytes()
        self.ok("add", group="Work", request_id="later", items=[{"text": "later item"}])
        (self.state_dir / "store.json").write_bytes(older)
        self.refused("read", "store_mismatch")
        self.refused("add", "store_mismatch", group="Work", items=[{"text": "x"}], request_id="r2")
        self.assertEqual(self.store_bytes(), older)
        self.assertEqual(self.ok("store", action="status")["state"], "mismatch")
        self.assertIn("left untouched", self.api.call_route("view")["warning"])

        foreign = json.loads(older)
        foreign["store_id"] = "s_" + "0" * 16
        foreign["generation"] = 999
        (self.state_dir / "store.json").write_text(json.dumps(foreign))
        self.refused("read", "store_mismatch")

    def test_export_and_restore_round_trip_into_a_fresh_installation(self):
        ids = self.build_lists()
        source_doc = json.loads(self.store_bytes())
        exported, path = self.export_file()
        self.assertIn("save it outside", self.api.call_route("view")["store"]["hint"])
        envelope = json.loads(path.read_text())
        self.assertEqual(envelope["format"], "smart_lists.export")
        self.assertEqual(envelope["sha256"], exported["sha256"])
        self.assertEqual(envelope["counts"], {"groups": 3, "entries": 4, "open": 2, "done": 1, "deleted": 1})
        self.assertEqual(json.loads(json.dumps(envelope["document"])), source_doc)
        self.assertIn("deletes", exported["note"])
        selection = self.ok("select", group="Home")["entries"]

        other_dir = pathlib.Path(self._tmp.name) / "reinstalled"
        self.api = self.fresh_api(other_dir)
        self.refused("read", "store_not_initialized")
        restored = self.ok("store", action="restore", file=str(path))
        self.assertEqual((restored["source"]["kind"], restored["backup"]), ("export", None))
        restored_doc = json.loads((other_dir / "store.json").read_bytes())
        assert_same_lists(self, source_doc, restored_doc)
        self.assertEqual(self.ok("select", group="Home")["entries"], selection)
        self.assertEqual(self.ok("read", status="deleted")["entries"][0]["id"], ids["soap"])
        self.assertEqual(self.ok("read", group="Home / Groceries")["entries"][0]["due"],
                         {"raw": "2026-10-01T18:00+03:00", "at": "2026-10-01T18:00:00+03:00"})

        retry = self.ok("add", group="Home / Groceries", request_id="capture-1",
                        items=[{"text": "  2 l oat milk ", "due": "2026-10-01T18:00+03:00"}, {"text": "bread"},
                               {"text": "soap", "due": "tomorrow"}])
        self.assertTrue(retry["replayed"], "the journal travels with the export")
        self.assertEqual(self.ok("read", status="all")["total"], 3)

    def test_restore_never_overwrites_silently_and_backs_up_first(self):
        self.build_lists()
        _exported, path = self.export_file()
        self.ok("add", group="Work", request_id="after-export", items=[{"text": "written after export"}])
        current = self.store_bytes()

        refusal = self.refused("store", "store_exists", action="restore", file=path.name)
        self.assertIn("3 groups and 5 entries", refusal["error"]["message"])
        self.assertIn("3 groups, 4 entries", refusal["error"]["message"])
        self.assertEqual(self.store_bytes(), current)

        restored = self.ok("store", action="restore", file=path.name, replace=True)
        backup = pathlib.Path(restored["backup"])
        self.assertEqual(backup.read_bytes(), current, "the replaced store is kept byte-for-byte")
        self.assertEqual(restored["replaced"]["entries"], 5)
        self.assertNotIn("written after export", [e["text"] for e in self.ok("read", status="all")["entries"]])
        self.refused("add", "request_expired", group="Work", request_id="after-export",
                     items=[{"text": "written after export"}])

        back = self.ok("store", action="restore", file=backup.name, replace=True)
        self.assertEqual(back["source"]["kind"], "store_document")
        self.assertIn("written after export", [e["text"] for e in self.ok("read", status="all")["entries"]])
        names = {item["file"] for item in self.ok("store", action="status")["backups"]}
        self.assertTrue({path.name, backup.name} <= names)

    def test_invalid_or_tampered_exports_are_refused_without_writing(self):
        self.build_lists()
        _exported, path = self.export_file()
        envelope = json.loads(path.read_text())
        current = self.store_bytes()

        def variant(name, mutate):
            data = copy.deepcopy(envelope)
            mutate(data)
            target = self.state_dir / "backups" / f"{name}.json"
            target.write_text(json.dumps(data) if not isinstance(data, str) else data)
            return target.name

        def edit_text(data):
            next(iter(data["document"]["entries"].values()))["text"] = "edited"

        def orphan(data):
            next(iter(data["document"]["entries"].values()))["group_id"] = "g_0000000000"
            data["sha256"] = self.module.lists_core._digest(data["document"])

        def wrong_counts(data):
            data["counts"]["entries"] = 99

        cases = {
            "tampered": edit_text,
            "orphan": orphan,
            "counts": wrong_counts,
            "newer": lambda data: data.update(format_version=2),
            "foreign": lambda data: data.update(format="something.else"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self.refused("store", "invalid_export", action="restore", file=variant(name, mutate), replace=True)
                self.assertEqual(self.store_bytes(), current)
        (self.state_dir / "backups" / "garbage.json").write_text("{not json")
        self.refused("store", "invalid_export", action="restore", file="garbage.json", replace=True)
        self.refused("store", "not_found", action="restore", file="absent.json")
        self.refused("store", "invalid_input", action="restore", file="../store.json")
        self.refused("store", "invalid_input", action="restore", file="notes.txt")
        self.refused("store", "invalid_input", action="restore")
        self.assertEqual(self.store_bytes(), current)

    def test_unreadable_store_is_replaced_only_with_a_protected_copy(self):
        self.build_lists()
        _exported, path = self.export_file()
        (self.state_dir / "store.json").write_bytes(b"{damaged")
        self.refused("read", "store_unreadable")
        refusal = self.refused("store", "store_exists", action="restore", file=path.name)
        self.assertIn("unreadable", refusal["error"]["message"])
        restored = self.ok("store", action="restore", file=path.name, replace=True)
        self.assertEqual(pathlib.Path(restored["backup"]).read_bytes(), b"{damaged")
        self.assertEqual(self.ok("read", status="all")["total"], 3)

    def test_widget_download_and_pasted_restore(self):
        self.build_lists()
        render = self.api.tabs["lists"]["render"]
        download = [c for c in iter_components(render["components"]) if c["type"] == "file"]
        self.assertEqual([(c["route"], c["condition_key"]) for c in download], [("export", "store.exportable")])
        view = self.api.call_route("view")
        self.assertTrue(view["store"]["exportable"])
        self.assertIn("Download a full export", view["store"]["hint"])
        envelope = self.api.call_route("export")
        self.assertEqual(envelope["counts"]["entries"], 4)
        self.assertIn("0 changes since", self.api.call_route("view")["store"]["last_export"])

        other = self.fresh_api(pathlib.Path(self._tmp.name) / "second")
        with self.assertRaises(self.module.ListError):
            other.call_route("export")  # the host turns this into a failed download
        empty = other.call_route("store", "POST", {"action": "restore", "export_json": ""})
        self.assertFalse(empty["ok"])
        self.assertIn("Paste", empty["warning"])
        restored = other.call_route("store", "POST", {"action": "restore", "export_json": json.dumps(envelope),
                                                      "replace": False})
        self.assertTrue(restored["ok"], restored)
        self.assertIn("Restored 3 groups and 4 entries", restored["notice"])
        self.assertEqual(restored["stats"], {"groups": 3, "open": 2, "done": 1, "deleted": 1})
        again = other.call_route("store", "POST", {"action": "restore", "export_json": json.dumps(envelope)})
        self.assertFalse(again["ok"])
        self.assertIn("replace", again["warning"])
        fresh = other.call_route("store", "POST", {"action": "init", "replace": True})
        self.assertTrue(fresh["ok"], fresh)
        self.assertIn("copied to", fresh["notice"])
        self.assertEqual(fresh["stats"]["groups"], 0)

    def test_store_saved_by_version_0_1_0_is_read_and_upgraded_on_first_change(self):
        self.state_dir.mkdir(parents=True)
        legacy = {
            "schema_version": 1, "next_seq": 3, "updated_at": "2026-09-01T10:00:00+00:00",
            "groups": {"g_00000000a1": {"id": "g_00000000a1", "name": "Home", "parent_id": None,
                                        "created_at": "2026-09-01T10:00:00+00:00",
                                        "updated_at": "2026-09-01T10:00:00+00:00"}},
            "entries": {
                eid: {"id": eid, "seq": seq, "group_id": "g_00000000a1", "text": text, "status": status,
                      "due": None, "source": "chat", "created_at": "2026-09-01T10:00:00+00:00",
                      "updated_at": "2026-09-01T10:00:00+00:00",
                      "completed_at": "2026-09-01T11:00:00+00:00" if status == "done" else None}
                for eid, seq, text, status in (("e_00000000b1", 1, "milk", "open"),
                                               ("e_00000000b2", 2, "bread", "done"))},
            "requests": {"old-1": {"op": "add", "fingerprint": "f" * 64, "at": "2026-09-01T10:00:00+00:00",
                                   "result": {"op": "add", "added": [{"id": "e_00000000b1"}]}}},
        }
        malformed_legacy = json.loads(json.dumps(legacy))
        malformed_legacy["requests"]["old-1"] = None
        (self.state_dir / "store.json").write_text(json.dumps(malformed_legacy))
        self.assertEqual(self.ok("store", action="status")["state"], "unreadable")
        self.refused("add", "store_unreadable", group="Home", items=[{"text": "eggs"}], request_id="bad-legacy")
        (self.state_dir / "store.json").write_text(json.dumps(legacy))
        self.assertEqual(self.ok("store", action="status")["state"], "ready")
        self.assertEqual([e["text"] for e in self.ok("read", status="all")["entries"]], ["milk", "bread"])
        self.assertEqual(json.loads(self.store_bytes())["schema_version"], 1, "reads never rewrite the store")
        self.ok("add", group="Home", request_id="new-1", items=[{"text": "eggs"}])
        upgraded = json.loads(self.store_bytes())
        self.assertEqual((upgraded["schema_version"], upgraded["generation"]), (3, 1))
        self.assertIsNotNone(upgraded["retired_requests"], "unsafe legacy replay is retired")
        self.refused("add", "request_expired", group="Home", items=[{"text": "milk"}], request_id="old-1")
        self.assertRegex(upgraded["store_id"], r"^s_[0-9a-f]{16}$")
        for eid, entry in legacy["entries"].items():
            self.assertLessEqual(entry.items(), upgraded["entries"][eid].items())
        self.assertTrue((self.state_dir / "store_meta.json").exists())
        self.refused("store", "store_exists", action="init")


class DeletionTests(SkillCase):
    def test_delete_is_a_reversible_trash_and_erase_is_explicit(self):
        self.ok("group", action="create", name="Home")
        milk, bread, eggs = self.add("Home", "milk", "bread", "eggs", request_id="cap-1")
        self.ok("complete", entry_ids=[bread])
        deleted = self.ok("delete", entry_ids=[milk, bread], request_id="del-1")
        self.assertEqual([e["id"] for e in deleted["deleted"]], [milk, bread])
        self.assertTrue(all(e["deleted_at"] for e in deleted["deleted"]))

        self.assertEqual([e["id"] for e in self.ok("read", status="all")["entries"]], [eggs])
        self.assertEqual([e["id"] for e in self.ok("read", status="deleted")["entries"]], [milk, bread])
        self.assertEqual([e["entry_id"] for e in self.ok("select", group="Home")["entries"]], [eggs])
        row = self.ok("read")["groups"][0]
        self.assertEqual((row["open"], row["done"], row["deleted"]), (1, 0, 2))
        view = self.api.call_route("view")
        self.assertEqual(view["stats"], {"groups": 1, "open": 1, "done": 0, "deleted": 2})
        self.assertEqual({row["id"] for row in view["deleted_rows"]}, {milk, bread})
        for tool, args in (("update", {"entry_id": milk, "text": "x"}), ("complete", {"entry_ids": [milk]}),
                           ("move", {"entry_ids": [milk], "to_group": "Home"})):
            with self.subTest(tool=tool):
                self.assertIn("undo", self.refused(tool, "conflict", **args)["error"]["message"])
        self.refused("delete", "conflict", entry_ids=[eggs], action="erase")

        again = self.ok("delete", entry_ids=[milk, bread], request_id="del-1")
        self.assertTrue(again["replayed"])
        undone = self.ok("delete", entry_ids=[bread], action="undo")
        self.assertEqual((undone["undeleted"][0]["status"], undone["undeleted"][0]["deleted_at"]), ("done", None))
        self.assertEqual([e["id"] for e in self.ok("read", status="done")["entries"]], [bread])

        erased = self.ok("delete", entry_ids=[milk], action="erase", request_id="erase-1")
        self.assertEqual(erased["erased"][0]["text"], "milk", "the result names what was erased")
        self.assertNotIn(b'"milk"', self.store_bytes(), "erased text is gone from the store and its journal")
        self.refused("delete", "not_found", entry_ids=[milk], action="undo")
        replay = self.ok("delete", entry_ids=[milk], action="erase", request_id="erase-1")
        self.assertEqual((replay["replayed"], replay["erased"]), (True, [{"id": milk, "gone": True}]))

        capture = self.ok("add", group="Home", request_id="cap-1",
                          items=[{"text": "milk"}, {"text": "bread"}, {"text": "eggs"}])
        self.assertTrue(capture["replayed"], "retrying the capture must not bring erased text back")
        self.assertEqual(capture["added"][0], {"id": milk, "gone": True})
        self.assertEqual(self.ok("read", status="all")["total"], 2)

    def test_group_with_deleted_entries_cannot_be_deleted_until_they_are_erased(self):
        self.ok("group", action="create", name="Trip")
        entry = self.add("Trip", "sunscreen")[0]
        self.ok("delete", entry_ids=[entry])
        message = self.refused("group", "conflict", action="delete", group="Trip")["error"]["message"]
        self.assertIn("1 deleted entries", message)
        self.ok("delete", entry_ids=[entry], action="erase")
        self.ok("group", action="delete", group="Trip")

    def test_widget_delete_undo_and_erase(self):
        self.ok("group", action="create", name="Home")
        entry = self.add("Home", "doormat")[0]
        combined = self.api.call_route("edit", "POST", {"entry_id": entry, "status": "delete", "text": "mat"})
        self.assertFalse(combined["ok"])
        deleted = self.api.call_route("edit", "POST", {"entry_id": entry, "status": "delete"})
        self.assertTrue(deleted["ok"], deleted)
        self.assertIn("Undo delete", deleted["notice"])
        self.assertEqual([row["id"] for row in deleted["deleted_rows"]], [entry])
        undone = self.api.call_route("edit", "POST", {"entry_id": entry, "status": "undo"})
        self.assertEqual(([row["id"] for row in undone["open_rows"]], undone["deleted_rows"]), ([entry], []))
        refused = self.api.call_route("edit", "POST", {"entry_id": entry, "status": "erase"})
        self.assertFalse(refused["ok"])
        self.assertIn("delete", refused["warning"])
        self.api.call_route("edit", "POST", {"entry_id": entry, "status": "delete"})
        erased = self.api.call_route("edit", "POST", {"entry_id": entry, "status": "erase"})
        self.assertIn("permanently", erased["notice"])
        self.assertEqual(erased["stats"], {"groups": 1, "open": 0, "done": 0, "deleted": 0})


class ReplayWindowTests(SkillCase):
    def test_journal_eviction_never_turns_a_retry_into_a_silent_duplicate(self):
        self.module.lists_core.MAX_REQUESTS = 3
        self.ok("group", action="create", name="Inbox")
        first = self.ok("add", group="Inbox", request_id="cap-old", items=[{"text": "paint"}])
        entry = first["added"][0]["id"]
        self.ok("complete", entry_ids=[entry], request_id="done-old")
        for index in range(4):
            self.ok("add", group="Inbox", request_id=f"filler-{index}", items=[{"text": f"filler {index}"}])
        journal = json.loads(self.store_bytes())["requests"]
        self.assertNotIn("cap-old", journal)
        self.assertNotIn("done-old", journal)

        self.refused("add", "request_expired", group="Inbox", request_id="cap-old", items=[{"text": "paint"}])
        self.refused("add", "request_expired", group="Inbox", request_id="cap-old", items=[{"text": "changed"}])
        self.ok("complete", entry_ids=[entry], done=False, request_id="reopen-now")
        self.refused("complete", "request_expired", entry_ids=[entry], request_id="done-old")
        self.assertEqual(self.ok("read", status="open")["total"], 5, "the late retry did not re-complete it")

        self.ok("delete", entry_ids=[entry])
        self.ok("delete", entry_ids=[entry], action="erase")
        for index in range(4, 8):
            self.ok("add", group="Inbox", request_id=f"filler-{index}", items=[{"text": f"filler {index}"}])
        self.refused("add", "request_expired", group="Inbox", request_id="cap-old", items=[{"text": "paint"}])
        self.assertEqual(self.ok("read", status="all")["total"], 8)


def host_download_name(component):
    """Mirror of the host's widget download naming (web/modules/widgets.js:
    safeMediaSrc builds route + ``query``; filenameFromWidgetUrl prefers the
    ``filename`` query parameter, else the last URL path segment)."""
    query = urllib.parse.urlencode(component.get("query") or {})
    url = f"/api/extensions/smart_lists/{component['route']}" + (f"?{query}" if query else "")
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(parsed.query)
    for key in ("filename", "image_id", "clip_id"):
        if params.get(key) and params[key][0]:
            return params[key][0].split("/")[-1] or "download"
    return [part for part in parsed.path.split("/") if part][-1]


class FakeMsvcrt:
    """Stand-in for Windows ``msvcrt.locking``: byte-range locks that conflict
    between file handles (even in one process), as LockFile does."""

    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self, always_locked=False):
        self.always_locked = always_locked
        self._guard = threading.Lock()
        self._owners = {}
        self.locks = self.unlocks = self.refusals = 0

    def locking(self, fd, mode, nbytes):
        assert nbytes == 1 and os.lseek(fd, 0, os.SEEK_CUR) == 0, "lock the first byte from offset 0"
        key = os.fstat(fd).st_ino
        with self._guard:
            if mode == self.LK_NBLCK:
                if self.always_locked or key in self._owners:
                    self.refusals += 1
                    raise OSError(errno.EACCES, "Locking violation")
                self._owners[key] = fd
                self.locks += 1
            elif mode == self.LK_UNLCK:
                assert self._owners.get(key) == fd, "unlock by the holder only"
                del self._owners[key]
                self.unlocks += 1
            else:
                raise AssertionError(f"unexpected mode {mode}")


class DuplicateReportTests(SkillCase):
    def test_duplicate_report_is_capped_with_total_and_truncated(self):
        core = self.module.lists_core
        self.ok("group", action="create", name="Home")
        first = self.add("Home", *(["milk"] * 7), request_id="batch")
        done, deleted = self.add("Home", "MILK", " milk ")
        self.ok("complete", entry_ids=[done])
        self.ok("delete", entry_ids=[deleted])
        result = self.ok("add", group="Home", items=[{"text": "Milk"}, {"text": "bread"}], request_id="later")
        report = result["possible_duplicates"]
        self.assertEqual(len(report), 1, "only the entry with open same-text matches is reported")
        self.assertEqual(report[0], {"entry_id": result["added"][0]["id"],
                                     "same_text_open_entries": first[:core.MAX_DUPLICATE_IDS],
                                     "total": 7, "truncated": True})
        batch = json.loads(self.store_bytes())["requests"]["batch"]["result"]
        self.assertEqual(batch["possible_duplicates"], [])
        replay = self.ok("add", group="Home", items=[{"text": "milk"}] * 7, request_id="batch")
        self.assertTrue(replay["duplicates_omitted_on_replay"])
        self.assertEqual(self.ok("read", group="Home", limit=500)["total"], 9)


class ImportValidationTests(SkillCase):
    """Restore accepts only documents this skill could have written."""

    def setUp(self):
        super().setUp()
        core = self.module.lists_core
        self.core = core
        self.ok("group", action="create", name="Home")
        self.ok("group", action="create", name="Groceries", parent="Home")
        self.ok("group", action="create", name="Work")
        self.ids = self.add("Home / Groceries", "milk", "bread", request_id="capture-1")
        self.ok("complete", entry_ids=[self.ids[1]], request_id="done-1")
        exported = self.ok("store", action="export")
        self.envelope = json.loads(pathlib.Path(exported["file"]).read_text())
        self.current = self.store_bytes()

    def write_variant(self, name, mutate, *, fix_checksum=True, raw=None):
        data = copy.deepcopy(self.envelope)
        mutate(data["document"])
        if fix_checksum:
            data["sha256"] = self.core._digest(data["document"])
        target = self.state_dir / "backups" / f"variant-{name}.json"
        target.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
        return target.name

    def group_id(self, doc, name):
        return next(gid for gid, group in doc["groups"].items() if group["name"] == name)

    def assert_refused(self, name, mutate, **kwargs):
        with self.subTest(case=name):
            message = self.refused("store", "invalid_export", action="restore",
                                   file=self.write_variant(name, mutate, **kwargs), replace=True)["error"]["message"]
            self.assertIn("Nothing was restored", message)
            self.assertEqual(self.store_bytes(), self.current)
            return message

    def test_every_field_is_validated_before_restore(self):
        entry = lambda doc, index=0: doc["entries"][self.ids[index]]  # noqa: E731
        request = lambda doc: doc["requests"]["capture-1"]  # noqa: E731
        nested = {"level": 0}
        for level in range(1, 10):
            nested = {"level": level, "inner": nested}
        cases = {
            "sibling_names_differ_only_in_case": lambda d: d["groups"][self.group_id(d, "Work")].update(name="HOME"),
            "parent_cycle": lambda d: d["groups"][self.group_id(d, "Home")].update(
                parent_id=self.group_id(d, "Groceries")),
            "name_with_slash": lambda d: d["groups"][self.group_id(d, "Work")].update(name="Work/Office"),
            "name_too_long": lambda d: d["groups"][self.group_id(d, "Work")].update(name="w" * 81),
            "name_looks_like_id": lambda d: d["groups"][self.group_id(d, "Work")].update(name="g_0123456789"),
            "name_not_text": lambda d: d["groups"][self.group_id(d, "Work")].update(name=5),
            "name_untrimmed": lambda d: d["groups"][self.group_id(d, "Work")].update(name=" Work "),
            "group_unknown_field": lambda d: d["groups"][self.group_id(d, "Work")].update(color="red"),
            "text_too_long": lambda d: entry(d).update(text="x" * 1001),
            "text_blank": lambda d: entry(d).update(text="   "),
            "text_not_text": lambda d: entry(d).update(text=["milk"]),
            "due_too_long": lambda d: entry(d).update(due={"raw": "x" * 121}),
            "due_instant_not_from_raw": lambda d: entry(d).update(
                due={"raw": "2026-10-01T18:00+03:00", "at": "2030-01-01T00:00:00+00:00"}),
            "due_extra_key": lambda d: entry(d).update(due={"raw": "tomorrow", "note": 1}),
            "due_plain_string": lambda d: entry(d).update(due="tomorrow"),
            "completed_at_number": lambda d: entry(d, 1).update(completed_at=5),
            "deleted_at_true": lambda d: entry(d).update(deleted_at=True),
            "created_at_list": lambda d: entry(d).update(created_at=[]),
            "source_too_long": lambda d: entry(d).update(source="s" * 33),
            "entry_request_id_bad": lambda d: entry(d).update(request_id="bad id!"),
            "entry_unknown_field": lambda d: entry(d).update(qty=2),
            "seq_repeated": lambda d: entry(d, 1).update(seq=entry(d)["seq"]),
            "seq_zero": lambda d: entry(d).update(seq=0),
            "seq_not_below_next_seq": lambda d: entry(d).update(seq=d["next_seq"]),
            "next_seq_bool": lambda d: d.update(next_seq=True),
            "generation_negative": lambda d: d.update(generation=-1),
            "store_id_malformed": lambda d: d.update(store_id="store-1"),
            "unknown_top_level": lambda d: d.update(extra={}),
            "request_id_bad": lambda d: d["requests"].update({"bad id!": request(d)}),
            "request_fingerprint_bad": lambda d: request(d).update(fingerprint="zz"),
            "request_op_blank": lambda d: request(d).update(op=""),
            "request_result_list": lambda d: request(d).update(result=[]),
            "request_result_too_deep": lambda d: request(d).update(result=nested),
            "request_unknown_field": lambda d: request(d).update(extra=1),
            "retired_not_base64": lambda d: d.update(retired_requests={"bits": self.core.BLOOM_BITS,
                                                                       "hashes": self.core.BLOOM_HASHES,
                                                                       "data": "!!!"}),
            "retired_wrong_length": lambda d: d.update(retired_requests={
                "bits": self.core.BLOOM_BITS, "hashes": self.core.BLOOM_HASHES,
                "data": base64.b64encode(zlib.compress(b"\x01" * 10)).decode()}),
            "retired_decompression_bomb": lambda d: d.update(retired_requests={
                "bits": self.core.BLOOM_BITS, "hashes": self.core.BLOOM_HASHES,
                "data": base64.b64encode(zlib.compress(b"\xff" * (8 << 20))).decode()}),
            "retired_other_parameters": lambda d: d.update(retired_requests={
                "bits": 64, "hashes": 7, "data": base64.b64encode(zlib.compress(b"\x00" * 8)).decode()}),
        }
        for name, mutate in cases.items():
            message = self.assert_refused(name, mutate)
            with self.subTest(case=name):
                self.assertIn("valid list store", message)
        self.assertEqual(self.ok("read", status="all")["total"], 2)

    def test_incomplete_or_malformed_journal_results_are_refused_before_replay(self):
        self.ok("group", action="create", name="Journal group", request_id="group-create-1")
        exported = self.ok("store", action="export")
        self.envelope = json.loads(pathlib.Path(exported["file"]).read_text())
        self.current = self.store_bytes()

        def capture(doc):
            return doc["requests"]["capture-1"]["result"]

        def complete(doc):
            return doc["requests"]["done-1"]["result"]

        def group_create(doc):
            return doc["requests"]["group-create-1"]["result"]["group"]

        cases = {
            "missing_record_stamp": lambda d: d["requests"]["capture-1"].pop("at"),
            "missing_result_op": lambda d: capture(d).pop("op"),
            "wrong_result_op": lambda d: capture(d).update(op="complete"),
            "missing_added": lambda d: capture(d).pop("added"),
            "missing_group": lambda d: capture(d).pop("group"),
            "empty_added": lambda d: capture(d).update(added=[]),
            "malformed_added_row": lambda d: capture(d)["added"][0].update(id=5),
            "incomplete_added_row": lambda d: capture(d)["added"][0].pop("group_id"),
            "missing_added_source": lambda d: capture(d)["added"][0].pop("source"),
            "missing_added_status": lambda d: capture(d)["added"][0].pop("status"),
            "missing_added_created_at": lambda d: capture(d)["added"][0].pop("created_at"),
            "added_wrong_group": lambda d: capture(d)["added"][0].update(
                group_id=self.group_id(d, "Work")),
            "added_wrong_status": lambda d: capture(d)["added"][0].update(status="done"),
            "duplicate_added_row": lambda d: capture(d)["added"].append(copy.deepcopy(capture(d)["added"][0])),
            "malformed_duplicate_report": lambda d: capture(d).update(possible_duplicates=[{}]),
            "duplicate_report_unknown_entry": lambda d: capture(d).update(possible_duplicates=[{
                "entry_id": "e_fffffffffe", "same_text_open_entries": [self.ids[0]],
                "total": 1, "truncated": False}]),
            "missing_changed": lambda d: complete(d).pop("changed"),
            "malformed_unchanged": lambda d: complete(d).update(unchanged=[5]),
            "changed_and_unchanged_overlap": lambda d: complete(d)["unchanged"].append(
                complete(d)["changed"][0]["id"]),
            "completed_with_open_status": lambda d: complete(d)["changed"][0].update(status="open"),
            "missing_created_group": lambda d: d["requests"]["group-create-1"]["result"].pop("group"),
            "missing_created_group_id": lambda d: group_create(d).pop("id"),
            "missing_created_group_parent": lambda d: group_create(d).pop("parent_id"),
            "malformed_created_group_parent": lambda d: group_create(d).update(parent_id=[]),
        }
        for name, mutate in cases.items():
            self.assert_refused(name, mutate)

        path = self.state_dir / "store.json"
        doc = json.loads(self.current)
        doc["requests"]["capture-1"]["result"].pop("added")
        damaged = json.dumps(doc).encode()
        path.write_bytes(damaged)
        self.refused("read", "store_unreadable")
        self.refused("add", "store_unreadable", group="Home / Groceries",
                     items=[{"text": "milk"}], request_id="capture-1")
        self.assertEqual(self.store_bytes(), damaged)

        doc = json.loads(self.current)
        doc["requests"]["group-create-1"]["result"]["group"].pop("parent_id")
        damaged = json.dumps(doc).encode()
        path.write_bytes(damaged)
        self.refused("group", "store_unreadable", action="create", name="Journal group",
                     request_id="group-create-1")
        self.assertEqual(self.store_bytes(), damaged)

    def test_full_legacy_schema_2_journal_results_remain_replayable(self):
        created = self.ok("group", action="create", name="Legacy group", request_id="legacy-group")
        exported = self.ok("store", action="export")
        self.envelope = json.loads(pathlib.Path(exported["file"]).read_text())

        def legacy(doc):
            doc["schema_version"] = 2
            doc["expired_requests"] = []
            doc.pop("retired_requests", None)
            added = doc["requests"]["capture-1"]["result"]["added"]
            for row in added:
                entry = doc["entries"][row["id"]]
                row.update(text=entry["text"], due=entry["due"], group_path="Home / Groceries")
            group = doc["requests"]["legacy-group"]["result"]["group"]
            group.update(name="Legacy group", path="Legacy group")

        restored = self.ok("store", action="restore", file=self.write_variant("legacy-full", legacy), replace=True)
        self.assertEqual(restored["counts"]["entries"], 2)
        replay = self.ok("add", group="Home / Groceries", items=[{"text": "milk"}, {"text": "bread"}],
                         request_id="capture-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(replay["added"]), 2)
        repeated = self.ok("group", action="create", name="Legacy group", request_id="legacy-group")
        self.assertTrue(repeated["replayed"])
        self.assertEqual(repeated["group"]["id"], created["group"]["id"])

    def test_malformed_unicode_and_deep_nesting_are_invalid_export(self):
        lone = json.dumps(self.envelope).replace('"milk"', '"mi\\ud800lk"')
        self.assertNotEqual(lone, json.dumps(self.envelope))
        self.assert_refused("lone_surrogate_in_text", lambda d: None, raw=lone)
        bare = json.dumps(json.loads(self.current)).replace('"Work"', '"Wo\\udc80rk"')
        self.assert_refused("lone_surrogate_in_bare_store", lambda d: None, raw=bare)
        self.assert_refused("deep_nesting", lambda d: None, raw="[" * 100000 + "]" * 100000)
        self.assert_refused("deep_nesting_unclosed", lambda d: None, raw="{\"a\":" * 100000)

        pasted = json.dumps(self.envelope, ensure_ascii=False).replace('"bread"', '"bre\ud800ad"')
        view = self.api.call_route("store", "POST", {"action": "restore", "export_json": pasted, "replace": True})
        self.assertFalse(view["ok"])
        self.assertIn("not valid Unicode", view["warning"])
        self.assertEqual(view["error"], view["warning"])
        self.assertEqual(self.store_bytes(), self.current)

    def test_corrupt_store_file_is_refused_without_breaking_the_widget(self):
        path = self.state_dir / "store.json"
        good = json.loads(self.current)
        def mixed_completed_at(doc):  # two done entries: sorting them compared int with str
            doc["entries"][self.ids[0]].update(status="done", completed_at="2026-09-01T10:00:00+00:00")
            doc["entries"][self.ids[1]].update(completed_at=5)

        corruptions = {
            "completed_at_number": mixed_completed_at,
            "lone_surrogate": None,
            "deep_nesting": None,
            "duplicate_siblings": lambda d: d["groups"][self.group_id(d, "Work")].update(name="home"),
        }
        for name, mutate in corruptions.items():
            with self.subTest(case=name):
                if name == "lone_surrogate":
                    content = self.current.decode("utf-8").replace('"milk"', '"mi\\ud800lk"').encode("utf-8")
                elif name == "deep_nesting":
                    content = b"[" * 100000 + b"]" * 100000
                else:
                    doc = copy.deepcopy(good)
                    mutate(doc)
                    content = json.dumps(doc).encode("utf-8")
                path.write_bytes(content)
                self.refused("read", "store_unreadable")
                self.refused("add", "store_unreadable", group="Work", items=[{"text": "x"}], request_id="r")
                view = self.api.call_route("view")  # raised TypeError for completed_at=5 before
                self.assertFalse(view["ok"])
                self.assertIn("left untouched", view["warning"])
                self.assertEqual(self.ok("store", action="status")["state"], "unreadable")
                self.assertEqual(self.store_bytes(), content)
        path.write_bytes(self.current)
        self.assertEqual(self.ok("read", status="all")["total"], 2)


class ExportRecordTests(SkillCase):
    def fail_export_records(self):
        store = self.module._service().store
        original = store._write_meta

        def write_meta(doc, **kwargs):
            if "last_export" in kwargs:
                raise OSError("disk full")
            return original(doc, **kwargs)

        return mock.patch.object(store, "_write_meta", side_effect=write_meta)

    def test_unrecorded_tool_export_is_delivered_and_reported(self):
        self.ok("group", action="create", name="Home")
        self.add("Home", "milk")
        with self.fail_export_records():
            exported = self.ok("store", action="export")
        self.assertFalse(exported["export_recorded"])
        self.assertIn("complete and usable", exported["warning"])
        self.assertIn("OSError", exported["warning"])
        status = self.ok("store", action="status")
        self.assertIsNone(status["last_export"])
        self.assertEqual(status["export_unrecorded"]["at"], exported["exported_at"])
        view = self.api.call_route("view")
        self.assertTrue(view["ok"], "nothing was refused")
        self.assertIn("not include it", view["warning"])
        self.assertEqual(view["store"]["export_warning"], view["warning"])
        self.assertIn("not recorded", view["store"]["last_export"])

        other = self.fresh_api(pathlib.Path(self._tmp.name) / "elsewhere")
        restored = other.call_tool("store", action="restore", file=exported["file"])
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(other.call_tool("read")["total"], 1)

    def test_unrecorded_widget_download_is_delivered_and_reported(self):
        self.ok("group", action="create", name="Home")
        self.add("Home", "milk")
        with self.fail_export_records():
            envelope = self.api.call_route("export", query={"filename": "smart-lists-export.json"})
        self.assertEqual((envelope["format"], envelope["counts"]["entries"]), ("smart_lists.export", 1))
        view = self.api.call_route("view")
        self.assertTrue(view["ok"])
        self.assertTrue(view["store"]["export_warning"])
        self.assertEqual(view["store"]["last_export"].split(";")[0], "Never")

        other = self.fresh_api(pathlib.Path(self._tmp.name) / "elsewhere")
        restored = other.call_route("store", "POST", {"action": "restore", "export_json": json.dumps(envelope)})
        self.assertTrue(restored["ok"], restored)

        self.api = self.fresh_api()
        self.module._service().store.export_record_failure = {"at": "earlier", "file": "", "error": "OSError"}
        self.api.call_route("export")
        cleared = self.api.call_route("view")
        self.assertEqual((cleared["warning"], cleared["store"]["export_warning"]), ("", ""))
        self.assertIn("0 changes since", cleared["store"]["last_export"])

    def test_download_button_names_the_file_as_json(self):
        render = self.api.tabs["lists"]["render"]
        declared = [render]
        if yaml is not None:
            declared.append(yaml.safe_load(manifest_text().split("---", 2)[1])["ui_tab"]["render"])
        for source in declared:
            files = [c for c in iter_components(source["components"]) if c["type"] == "file"]
            self.assertEqual(len(files), 1)
            self.assertEqual(host_download_name(files[0]), "smart-lists-export.json")
            without_query = dict(files[0], query={})
            self.assertEqual(host_download_name(without_query), "export", "why the query is needed")
        self.ok("group", action="create", name="Home")
        body = self.api.call_route("export", query=files[0]["query"])
        self.assertEqual(body["format"], "smart_lists.export")

    def test_damaged_sentinel_fields_never_break_status_views(self):
        meta_path = self.state_dir / "store_meta.json"
        meta = json.loads(meta_path.read_text())
        meta.update(last_export={"at": "2026-09-01T00:00:00+00:00", "generation": "abc", "sha256": 1, "file": None},
                    counts={"groups": "many"}, written_at=["x"])
        meta_path.write_text(json.dumps(meta))
        view = self.api.call_route("view")
        self.assertTrue(view["ok"], view)
        self.assertEqual((view["store"]["last_export"], view["store"]["last_written"]), ("Never", "—"))
        meta_path.write_text("{truncated")
        self.assertTrue(self.api.call_route("view")["ok"])
        self.ok("group", action="create", name="Home")
        self.assertEqual(json.loads(meta_path.read_text())["generation"],
                         json.loads(self.store_bytes())["generation"])


class StoreWriteFailureTests(SkillCase):
    def store_writes_fail(self, store):
        original = store._write_atomic

        def write_atomic(path, data):
            if path == store.path:
                raise OSError("disk full")
            return original(path, data)

        return mock.patch.object(store, "_write_atomic", side_effect=write_atomic)

    def test_failed_store_write_keeps_the_previous_store_ready(self):
        self.ok("group", action="create", name="Home")
        before, meta_before = self.store_bytes(), (self.state_dir / "store_meta.json").read_bytes()
        store = self.module._service().store
        with self.store_writes_fail(store):
            self.refused("add", "store_io", group="Home", items=[{"text": "milk"}], request_id="r1")
        self.assertEqual(self.store_bytes(), before)
        self.assertEqual((self.state_dir / "store_meta.json").read_bytes(), meta_before, "sentinel put back")
        self.assertEqual(self.ok("store", action="status")["state"], "ready")
        self.assertFalse(self.ok("add", group="Home", items=[{"text": "milk"}], request_id="r1")["replayed"])
        self.assertIsNone(json.loads((self.state_dir / "store_meta.json").read_text())["pending"])

    def test_interrupted_write_loads_the_last_committed_store_not_a_mismatch(self):
        self.ok("group", action="create", name="Home")
        older = self.store_bytes()
        self.add("Home", "milk")
        committed = self.store_bytes()
        store = self.module._service().store
        with self.store_writes_fail(store), mock.patch.object(store, "_put_back_meta"):
            self.refused("add", "store_io", group="Home", items=[{"text": "bread"}], request_id="r2")
        meta = json.loads((self.state_dir / "store_meta.json").read_text())
        doc = json.loads(committed)
        self.assertEqual(meta["generation"], doc["generation"] + 1, "the sentinel ran ahead, as after a crash")
        self.assertEqual(meta["pending"]["generation"], doc["generation"])
        self.assertEqual(self.store_bytes(), committed)

        reloaded = self.fresh_api()
        self.assertEqual(reloaded.call_tool("store", action="status")["state"], "ready")
        self.assertEqual([e["text"] for e in reloaded.call_tool("read")["entries"]], ["milk"])
        self.assertTrue(reloaded.call_route("view")["ok"])
        added = reloaded.call_tool("add", group="Home", items=[{"text": "bread"}], request_id="r2")
        self.assertTrue(added["ok"], added)
        self.assertEqual(json.loads(self.store_bytes())["generation"], meta["generation"])

        (self.state_dir / "store.json").write_bytes(older)
        self.assertEqual(self.refused("read", "store_mismatch")["error"]["code"], "store_mismatch")

    def test_commit_marker_failure_keeps_the_committed_change(self):
        self.ok("group", action="create", name="Home")
        store = self.module._service().store
        original = store._write_meta

        def write_meta(doc, **kwargs):
            if kwargs.get("pending", "absent") is None and "last_export" not in kwargs:
                raise OSError("disk full")
            return original(doc, **kwargs)

        with mock.patch.object(store, "_write_meta", side_effect=write_meta):
            self.assertTrue(self.ok("add", group="Home", items=[{"text": "milk"}], request_id="r1")["added"])
        self.assertIsNotNone(json.loads((self.state_dir / "store_meta.json").read_text())["pending"])
        self.assertEqual(self.ok("store", action="status")["state"], "ready")
        self.assertTrue(self.ok("add", group="Home", items=[{"text": "milk"}], request_id="r1")["replayed"])
        self.ok("add", group="Home", items=[{"text": "bread"}], request_id="r2")
        self.assertIsNone(json.loads((self.state_dir / "store_meta.json").read_text())["pending"])

    def test_unreadable_sentinel_is_a_typed_refusal(self):
        meta_path = self.state_dir / "store_meta.json"
        meta_path.unlink()
        meta_path.mkdir()
        before = self.store_bytes()
        self.refused("read", "store_io")
        self.refused("store", "store_io", action="status")
        self.refused("group", "store_io", action="create", name="Home")
        view = self.api.call_route("view")
        self.assertFalse(view["ok"])
        self.assertNotIn("error", view)
        self.assertEqual(self.store_bytes(), before)


class FirstWriteFailureTests(SkillCase):
    INIT = False

    def test_failed_first_init_leaves_the_store_uninitialized(self):
        store = self.module._service().store
        original = store._write_atomic

        def write_atomic(path, data):
            if path == store.path:
                raise OSError("disk full")
            return original(path, data)

        with mock.patch.object(store, "_write_atomic", side_effect=write_atomic):
            self.refused("store", "store_io", action="init")
        self.assertFalse((self.state_dir / "store_meta.json").exists())
        self.assertEqual(self.ok("store", action="status")["state"], "uninitialized")
        self.assertEqual(self.ok("store", action="init")["generation"], 1)


class LockTests(SkillCase):
    @unittest.skipIf(os.name == "nt", "holds the lock with POSIX flock")
    def test_lock_wait_is_bounded_and_typed(self):
        import fcntl

        self.ok("group", action="create", name="Home")
        before = self.store_bytes()
        with open(self.state_dir / "store.lock", "a+b") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            with mock.patch.object(self.module.lists_core, "LOCK_TIMEOUT_SEC", 0.2):
                message = self.refused("add", "store_busy", group="Home", items=[{"text": "x"}],
                                       request_id="r1")["error"]["message"]
                self.assertIn("nothing was read or changed", message)
                self.assertFalse(self.api.call_route("view")["ok"])
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        self.assertEqual(self.store_bytes(), before)
        self.ok("add", group="Home", items=[{"text": "x"}], request_id="r1")

    def test_wait_behind_another_thread_is_bounded_too(self):
        self.ok("group", action="create", name="Home")
        store = self.module._service().store
        held, release = threading.Event(), threading.Event()

        def holder():
            with store._locked():
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        try:
            with mock.patch.object(self.module.lists_core, "LOCK_TIMEOUT_SEC", 0.1):
                self.refused("read", "store_busy")
        finally:
            release.set()
            thread.join()
        self.ok("read")

    def use_windows_lock(self, core, fake):
        return mock.patch.multiple(core, fcntl=None, msvcrt=fake)

    def test_windows_lock_serializes_writers_that_share_no_thread_lock(self):
        self.ok("group", action="create", name="Inbox")
        fake = FakeMsvcrt()
        cores = [self.module.lists_core, load_plugin().lists_core]
        services = [self.module._service(), cores[1].SmartLists(self.state_dir)]
        errors = []

        def worker(service, name):
            for step in range(10):
                try:
                    service.add("Inbox", [{"text": f"{name}-{step}"}], request_id=f"{name}-{step}")
                except Exception as exc:  # noqa: BLE001 - collected for the assertion
                    errors.append(exc)

        with self.use_windows_lock(cores[0], fake), self.use_windows_lock(cores[1], fake):
            threads = [threading.Thread(target=worker, args=(services[index % 2], f"w{index}"))
                       for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.ok("read", limit=500)["total"], 80)
        self.assertEqual(fake.locks, fake.unlocks)
        self.assertGreaterEqual(fake.locks, 80)

    def test_windows_lock_timeout_is_store_busy(self):
        self.ok("group", action="create", name="Inbox")
        before = self.store_bytes()
        core = self.module.lists_core
        fake = FakeMsvcrt(always_locked=True)
        with self.use_windows_lock(core, fake), mock.patch.object(core, "LOCK_TIMEOUT_SEC", 0.1):
            self.refused("add", "store_busy", group="Inbox", items=[{"text": "x"}], request_id="r1")
        self.assertGreater(fake.refusals, 1, "the lock is retried until the deadline")
        self.assertEqual(self.store_bytes(), before)


class ReplayFilterTests(SkillCase):
    def test_oversized_journal_is_accepted_and_trimmed_into_the_filter(self):
        core = self.module.lists_core
        self.ok("group", action="create", name="Home")
        for index in range(4):
            self.ok("add", group="Home", items=[{"text": f"item {index}"}], request_id=f"id-{index}")
        with mock.patch.object(core, "MAX_REQUESTS", 2):  # a later, smaller limit must not strand the store
            self.assertEqual(self.ok("store", action="status")["state"], "ready")
            self.ok("group", action="create", name="Work")  # no request id: still trims
            self.assertEqual(list(json.loads(self.store_bytes())["requests"]), ["id-2", "id-3"])
            self.refused("add", "request_expired", group="Home", items=[{"text": "item 0"}], request_id="id-0")
            self.assertTrue(self.ok("add", group="Home", items=[{"text": "item 3"}], request_id="id-3")["replayed"])

    def test_saturated_filter_refuses_safely_and_the_widget_still_edits(self):
        core = self.module.lists_core
        self.ok("group", action="create", name="Home")
        entry = self.add("Home", "milk")[0]
        path = self.state_dir / "store.json"
        doc = json.loads(path.read_text())
        doc["retired_requests"] = {"bits": core.BLOOM_BITS, "hashes": core.BLOOM_HASHES,
                                   "data": base64.b64encode(zlib.compress(b"\xff" * (core.BLOOM_BITS // 8))).decode()}
        path.write_text(json.dumps(doc))
        before = self.store_bytes()
        message = self.refused("add", "request_expired", group="Home", items=[{"text": "bread"}],
                               request_id="fresh-id")["error"]["message"]
        self.assertIn("100.0% of new ids", message)
        self.assertIn("new request_id", message)
        self.assertEqual(self.store_bytes(), before)
        replay = self.ok("store", action="status")["replay"]
        self.assertEqual((replay["false_refusal_rate"], replay["retired_ids_estimate"]), (1.0, None))
        edited = self.api.call_route("edit", "POST", {"entry_id": entry, "status": "done"})
        self.assertTrue(edited["ok"], edited)

    def test_schema_2_expired_digests_fold_into_the_filter(self):
        core = self.module.lists_core
        self.ok("group", action="create", name="Home")
        self.add("Home", "milk", request_id="kept")
        path = self.state_dir / "store.json"
        doc = json.loads(path.read_text())
        doc.pop("retired_requests")
        doc.update(schema_version=2, expired_requests=[core._request_digest("old-a"), core._request_digest("old-b")])
        path.write_text(json.dumps(doc))
        self.assertEqual(self.ok("store", action="status")["state"], "ready")
        self.assertEqual(json.loads(self.store_bytes())["schema_version"], 2, "reads never rewrite the store")
        self.refused("add", "request_expired", group="Home", items=[{"text": "x"}], request_id="old-a")
        self.assertTrue(self.ok("add", group="Home", items=[{"text": "milk"}], request_id="kept")["replayed"])
        self.ok("add", group="Home", items=[{"text": "eggs"}], request_id="new-1")
        upgraded = json.loads(self.store_bytes())
        self.assertEqual(upgraded["schema_version"], 3)
        self.assertNotIn("expired_requests", upgraded)
        self.refused("add", "request_expired", group="Home", items=[{"text": "x"}], request_id="old-b")

        exported = self.ok("store", action="export")
        other = self.fresh_api(pathlib.Path(self._tmp.name) / "elsewhere")
        self.assertTrue(other.call_tool("store", action="restore", file=exported["file"])["ok"])
        refused = other.call_tool("add", group="Home", items=[{"text": "x"}], request_id="old-a")
        self.assertEqual(refused["error"]["code"], "request_expired", "retired ids travel with the export")


class CounterLimitTests(SkillCase):
    def test_next_seq_refuses_whole_batch_before_overflow(self):
        self.ok("group", action="create", name="Home")
        path = self.state_dir / "store.json"
        doc = json.loads(path.read_text())
        doc["next_seq"] = self.module.lists_core.MAX_COUNTER - 2
        path.write_text(json.dumps(doc))
        before = self.store_bytes()
        self.refused("add", "limit", group="Home", items=[{"text": "a"}, {"text": "b"}], request_id="overflow")
        self.assertEqual(self.store_bytes(), before)
        one = self.ok("add", group="Home", items=[{"text": "a"}], request_id="fits")
        self.assertEqual(len(one["added"]), 1)
        self.refused("add", "limit", group="Home", items=[{"text": "b"}], request_id="overflow")

    def test_generation_refuses_writes_and_restore_before_backup(self):
        self.ok("group", action="create", name="Home")
        path = self.state_dir / "store.json"
        meta_path = self.state_dir / "store_meta.json"
        doc, meta = json.loads(path.read_text()), json.loads(meta_path.read_text())
        doc["generation"] = self.module.lists_core.MAX_COUNTER - 1
        meta["generation"] = doc["generation"]
        path.write_text(json.dumps(doc))
        meta_path.write_text(json.dumps(meta))
        before, backups = self.store_bytes(), list((self.state_dir / "backups").glob("*.json"))
        self.refused("group", "limit", action="create", name="Work", request_id="overflow")
        self.assertEqual(self.store_bytes(), before)
        self.refused("store", "limit", action="init", replace=True)
        self.assertEqual(self.store_bytes(), before)
        self.assertEqual(list((self.state_dir / "backups").glob("*.json")), backups)


class ToolResultBoundsTests(SkillCase):
    def test_single_oversized_scalar_is_marked_and_keeps_json_intact(self):
        payload = {"ok": False, "error": {"code": "not_found", "message": "long path " + "x" * 40000}}
        raw = self.module._tool_json(payload)
        self.assertLessEqual(len(raw.encode("utf-8")), 14500)
        bounded = json.loads(raw)
        self.assertTrue(bounded["output_truncated"])
        self.assertIn("error.message", bounded["truncated_fields"])
        self.assertEqual(payload["error"]["message"], "long path " + "x" * 40000)

        page = {"ok": True, "entries": [{"id": "e_0000000001", "text": "milk", "group_path": "g" * 40000}],
                "offset": 0, "next_offset": 1, "total": 1, "truncated": False}
        bounded = json.loads(self.module._tool_json(page))
        self.assertEqual(bounded["entries"][0]["id"], "e_0000000001")
        self.assertEqual(bounded["next_offset"], 1)
        self.assertIn("entries[0].group_path", bounded["truncated_fields"])

    def test_large_read_selection_add_and_batch_results_are_complete_json_under_host_cap(self):
        self.ok("group", action="create", name="Home")
        items = [{"text": f"item {index:03d} " + "x" * 900} for index in range(100)]

        def raw(name, **args):
            response = self.api.tools[name]["handler"](None, **args)
            self.assertLessEqual(len(response.encode("utf-8")), 14500, name)
            return json.loads(response)

        added = raw("add", group="Home", items=items, request_id="large-add")
        self.assertTrue(added["ok"])
        self.assertEqual(added["result_counts"]["added"]["total"], 100)
        self.assertTrue(added["result_counts"]["added"]["truncated"])
        replay = raw("add", group="Home", items=items, request_id="large-add")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.ok("read", group="Home", limit=1)["total"], 100)

        for tool, args in (("read", {"group": "Home", "limit": 100}),
                           ("select", {"group": "Home", "limit": 100})):
            first = raw(tool, **args)
            self.assertEqual(first["total"], 100)
            self.assertTrue(first["truncated"])
            self.assertGreater(first["next_offset"], 0)
            second = raw(tool, **args, offset=first["next_offset"])
            self.assertEqual(second["entries"][0]["text"], items[first["next_offset"]]["text"])

        ids = list(json.loads(self.store_bytes())["entries"])
        completed = raw("complete", entry_ids=ids, request_id="large-complete")
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["result_counts"]["changed"]["total"], 100)
        self.assertTrue(completed["result_counts"]["changed"]["truncated"])
        self.assertEqual(self.ok("read", group="Home", status="done", limit=1)["total"], 100)


if __name__ == "__main__":
    unittest.main()
