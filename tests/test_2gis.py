"""2GIS registration and password updates without a host or vendor request."""

import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import yaml


class JSONResponse:
    """Only the response serialization needed by the settings route."""

    def __init__(self, content):
        self.body = json.dumps(content).encode("utf-8")
        self.status_code = 200


# Starlette is provided by Ouroboros, but is not a Hub CI dependency.
responses = ModuleType("starlette.responses")
responses.JSONResponse = JSONResponse
with patch.dict(sys.modules, {"starlette.responses": responses}):
    plugin = importlib.import_module("skills.2gis.plugin")


class Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class PluginAPI:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.tools = {}
        self.routes = {}
        self.sections = {}

    def get_state_dir(self):
        return self.state_dir

    def register_tool(self, name, **spec):
        self.tools[name] = spec

    def register_route(self, name, **spec):
        self.routes[name] = spec

    def register_settings_section(self, name, **spec):
        self.sections[name] = spec

    def log(self, *_args):
        pass


MAP_KEY = "TWOGIS_MAP_API_KEY"
ROUTING_KEY = "TWOGIS_ROUTING_API_KEY"


class TwoGisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="2gis-settings-test-")
        self.addCleanup(temporary.cleanup)
        self.state_path = Path(temporary.name) / "settings.json"
        self.api = PluginAPI(Path(temporary.name))
        self.addCleanup(setattr, plugin, "_api", plugin._api)
        plugin.register(self.api)

    def save(self, payload):
        response = asyncio.run(self.api.routes["settings/save"]["handler"](Request(payload)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {
            "ok": True, "message": "Ключи 2ГИС сохранены.",
        })
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_manifest_and_registration_preserve_all_seven_tools(self):
        manifest_path = Path(__file__).resolve().parents[1] / "skills/2gis/SKILL.md"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8").split("---", 2)[1])
        self.assertEqual(manifest["plugin_api"], "2.0")
        expected = {"check", "geocode", "reverse_geocode", "search_poi", "route", "isochrone", "map"}
        self.assertEqual(set(self.api.tools), expected)
        self.assertEqual({tool["name"] for tool in manifest["tools"]}, expected)
        self.assertTrue(all(callable(tool["handler"]) for tool in self.api.tools.values()))
        self.assertEqual(self.api.routes["settings/save"]["methods"], ("POST",))

    def test_settings_form_allows_updating_either_password_alone(self):
        form = self.api.sections["2gis"]["schema"]["components"][0]
        self.assertEqual(form["route"], "settings/save")
        self.assertEqual(form["method"], "POST")
        fields = {field["name"]: field for field in form["fields"]}
        self.assertEqual(set(fields), {MAP_KEY, ROUTING_KEY})
        for field in fields.values():
            self.assertEqual(field["type"], "password")
            self.assertFalse(field.get("required", False))
            self.assertNotIn("value", field)

    def test_initial_save_stores_supplied_keys_and_ignores_unknown_fields(self):
        self.assertEqual(self.save({
            MAP_KEY: " synthetic-map ", ROUTING_KEY: " synthetic-routing ", "unknown": "ignored",
        }), {MAP_KEY: "synthetic-map", ROUTING_KEY: "synthetic-routing"})

    def test_updating_either_key_preserves_the_other_blank_password(self):
        initial = {MAP_KEY: "synthetic-map", ROUTING_KEY: "synthetic-routing"}
        for changed, unchanged in ((MAP_KEY, ROUTING_KEY), (ROUTING_KEY, MAP_KEY)):
            with self.subTest(changed=changed):
                self.save(initial)
                self.assertEqual(self.save({changed: " replacement ", unchanged: ""}), {
                    changed: "replacement", unchanged: initial[unchanged],
                })

    def test_blank_or_omitted_passwords_preserve_both_saved_keys(self):
        initial = {MAP_KEY: "synthetic-map", ROUTING_KEY: "synthetic-routing"}
        for payload in ({MAP_KEY: "", ROUTING_KEY: ""}, {MAP_KEY: "  ", ROUTING_KEY: "\t"}, {}):
            with self.subTest(payload=payload):
                self.save(initial)
                self.assertEqual(self.save(payload), initial)

    def test_initial_save_accepts_either_key_without_the_other(self):
        for supplied, blank in ((MAP_KEY, ROUTING_KEY), (ROUTING_KEY, MAP_KEY)):
            with self.subTest(supplied=supplied):
                self.state_path.unlink(missing_ok=True)
                self.assertEqual(self.save({supplied: "synthetic-key", blank: ""}), {
                    supplied: "synthetic-key",
                })


if __name__ == "__main__":
    unittest.main()
