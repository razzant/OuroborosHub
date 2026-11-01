"""The status route offers Connect rows only for an account it can name."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gh_presence.config import connection_account, validate, write_config

REPOSITORIES = [{"id": 303, "full_name": "example/project"}]


class RecordingAPI:
    def __init__(self, state_dir):
        self.state_dir, self.routes, self.methods = state_dir, {}, {}
        self.settings, self.tabs, self.companions, self.tools = {}, [], [], {}

    def get_runtime_info(self):
        return {"state_dir": str(self.state_dir)}

    def register_route(self, path, handler, methods):
        self.routes[path] = handler
        self.methods[path] = methods

    def register_settings_section(self, section_id, **kwargs):
        self.settings[section_id] = kwargs

    def register_ui_tab(self, *args, **kwargs):
        self.tabs.append((args, kwargs))

    def register_companion_process(self, name):
        self.companions.append(name)

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs


class StatusRouteTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="github-status-")
        self.addCleanup(scratch.cleanup)
        self.state = Path(scratch.name)
        spec = importlib.util.spec_from_file_location("github_status_plugin", ROOT / "plugin.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        self.api = RecordingAPI(self.state)
        plugin.register(self.api)

    def status(self, **config):
        write_config(self.state, config)
        response = asyncio.run(self.api.routes["status"](SimpleNamespace(query_params={})))
        return json.loads(response.body)

    def test_diagnostics_and_companion_survive_without_a_widget(self):
        self.assertEqual(self.api.tabs, [])
        self.assertEqual(self.api.companions, ["github_poller"])
        self.assertEqual(set(self.api.tools), {"github_place_message", "github_place_close", "github_place_status"})
        components = self.api.settings["github"]["schema"]["components"]
        self.assertTrue(any(c.get("route") == "settings" for c in components))
        self.assertTrue(any(c.get("route") == "probe" for c in components))
        self.assertFalse(any(c["type"] == "poll" for c in components))
        self.assertEqual(self.api.methods["status"], ("GET",))
        response = asyncio.run(self.api.routes["status"](SimpleNamespace(query_params={})))
        self.assertEqual(response.status_code, 200)
        self.assertIn("host_contract_ready", json.loads(response.body))
        tool_status = json.loads(self.api.tools["github_place_status"]["handler"]())
        self.assertIn("host_contract_ready", tool_status)

    def test_rows_wait_for_the_writer_and_never_carry_a_null_account(self):
        unknown = self.status(repositories=REPOSITORIES, notifications_user_id=202)
        self.assertIsNone(unknown["account_id"])
        self.assertEqual(unknown["connect"], [])

    def test_known_writer_offers_each_repository_and_account_wide_notifications(self):
        known = self.status(repositories=REPOSITORIES, notifications_user_id=202, expected_writer_id=101)
        account = connection_account(validate({"expected_writer_id": 101}))
        self.assertTrue(isinstance(account, str) and account)
        self.assertEqual(known["connect"], [
            {"account_id": account, "conversation_id": "repo.303", "label": "example/project"},
            {"account_id": account, "conversation_id": "", "label": "Account-wide notifications"}])
        without_notifications = self.status(repositories=REPOSITORIES, expected_writer_id=101)
        self.assertEqual([row["conversation_id"] for row in without_notifications["connect"]], ["repo.303"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
