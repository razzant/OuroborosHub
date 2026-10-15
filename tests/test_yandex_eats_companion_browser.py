"""Registered plugin -> loopback companion -> intercepted Chromium consumers.

No personal profile or Yandex request: every URL is fulfilled or aborted by
PlaywrightDriver's synthetic transport. Opt in with YANDEX_EATS_BROWSER_FIXTURE=1.
Call-count comparisons are mechanical fixtures, not live/model latency claims.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parents[1] / "skills" / "yandex-travel"
NAME = "yandex_registered_browser_fixture"
spec = importlib.util.spec_from_file_location(NAME, SKILL / "plugin.py", submodule_search_locations=[str(SKILL)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[NAME] = plugin
spec.loader.exec_module(plugin)
core = sys.modules[NAME + ".eats_session"]
driver = sys.modules.get(NAME + ".eats_driver")
if driver is None:
    import importlib
    driver = importlib.import_module(NAME + ".eats_driver")

EDA = "https://eda.yandex.kz"
PAGES = {
    EDA + "/": """<html><body><script>
      setTimeout(() => location.replace('/ready'), 1100);
    </script></body></html>""",
    EDA + "/ready": """<!doctype html><html><head><meta charset='utf-8'></head><body>
      <h1>Changing fixture menu</h1>
      <form role='search' onsubmit='event.preventDefault(); window.searches++;
        setTimeout(() => document.getElementById("results").textContent="New results", 1100)'>
      <input type='search' aria-label='Find'><button>Search now</button></form>
      <div id='results'></div>
      <article><h3>Fixture dish</h3><button id='add' onclick='window.adds++;
        setTimeout(() => document.getElementById("cart").textContent="Existing × 2; Added × " + window.adds, 1100)'>Add selected</button></article>
      <aside aria-label='Fixture cart'><div id='cart'>Existing × 2</div></aside>
      <button onclick='document.getElementById("add").textContent="Renamed action"'>Change markup</button>
      <button onclick='setTimeout(() => location.href="https://passport.yandex.kz/auth", 300)'>Login fixture</button>
      <script>window.adds=0;window.searches=0;</script>
      </body></html>""",
    "https://passport.yandex.kz/auth": "<html><body>PRIVATE LOGIN CONTENT<input value='private'></body></html>",
}


class API:
    def __init__(self, state):
        self.state, self.tools = state, {}
    def get_state_dir(self):
        return str(self.state)
    def register_companion_process(self, name):
        pass
    def register_tool(self, name, handler, **kwargs):
        self.tools[name] = handler


@unittest.skipUnless(os.environ.get("YANDEX_EATS_BROWSER_FIXTURE") == "1", "opt-in intercepted Chromium")
class RegisteredCompanionBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.stop = threading.Event()
        self.errors = []
        self.calls = 0
        def server():
            try:
                session = core.Session(lambda: driver.PlaywrightDriver(core.prepare_profile(self.state),
                    fixtures=PAGES, headless=True, channel="chrome"), state_dir=self.state)
                self.session = session
                core.serve(session, self.state, should_stop=self.stop.is_set)
            except Exception as exc:
                self.errors.append(exc)
        self.thread = threading.Thread(target=server)
        self.thread.start()
        self.addCleanup(self.shutdown)
        deadline = time.monotonic() + 10
        while not self.state.joinpath(*core.ENDPOINT).exists() and not self.errors and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.errors)
        self.assertTrue(self.state.joinpath(*core.ENDPOINT).exists())
        self.api = API(self.state)
        plugin.register(self.api)

    def shutdown(self):
        self.stop.set()
        self.thread.join(15)
        if self.thread.is_alive():
            raise AssertionError("fixture companion did not stop")
        self.assertFalse(self.errors, self.errors)
        self.assertFalse(self.state.joinpath(*core.ENDPOINT).exists())
        self.temp.cleanup()

    def call(self, short, **args):
        self.calls += 1
        return json.loads(self.api.tools["yandex_eats_" + short](None, **args))

    @staticmethod
    def chosen(observation, name):
        matches = [e for e in observation["elements"] if e["name"] == name]
        assert len(matches) == 1, matches
        return matches[0]["id"]

    def test_delayed_open_reuse_and_existing_cart_through_registered_companion(self):
        opened = self.call("open")
        self.assertEqual(opened["readiness"], "observed", opened)
        self.assertEqual(opened["observation"]["url"], EDA + "/ready")
        self.assertIn("Existing × 2", opened["observation"]["text"])
        reused = self.call("open")
        self.assertFalse(reused["launched"])
        self.assertEqual(self.session.launches, 1)
        self.assertIn("Existing × 2", reused["observation"]["text"])
        self.assertEqual(self.call("close")["status"], "closed")

    def test_delayed_cart_two_calls_vs_one_and_changed_markup_recovery(self):
        first = self.call("open")["observation"]
        begin = self.calls
        added = self.call("act", action="click", observation_id=first["observation_id"],
                          element_id=self.chosen(first, "Add selected"), intent="add_item")
        self.assertNotIn("Added × 1", added["observation"]["text"])
        # Baseline waits between calls, as a later model turn would; this is
        # deliberately NOT measured as a provider or live-site speedup.
        time.sleep(1.2)
        old = self.call("observe")["observation"]
        old_calls = self.calls - begin
        self.assertIn("Existing × 2; Added × 1", old["text"])

        fresh = self.call("open", home=True)["observation"]
        begin = self.calls
        new = self.call("act", action="click", observation_id=fresh["observation_id"],
                        element_id=self.chosen(fresh, "Add selected"), intent="add_item", wait_ms=1500)
        self.assertIn("Existing × 2; Added × 1", new["observation"]["text"])
        self.assertNotIn("Added × 2", new["observation"]["text"])
        self.assertEqual((old_calls, self.calls - begin), (2, 1))
        self.assertEqual(self.session.launches, 1)
        before = new["observation"]
        changed = self.call("act", action="click", observation_id=before["observation_id"],
                            element_id=self.chosen(before, "Change markup"), intent="choose")["observation"]
        stale = self.call("act", action="click", observation_id=before["observation_id"],
                          element_id=self.chosen(before, "Add selected"), intent="add_item", wait_ms=100)
        self.assertEqual((stale["code"], stale["effect"]), ("stale_observation", "none"))
        latest = stale["observation"]
        self.assertIn("Renamed action", [e["name"] for e in latest["elements"]])
        # Choose anew from fresh evidence, not a cached label or old index.
        self.assertNotEqual(latest["observation_id"], changed["observation_id"])
        captured = self.call("capture", observation_id=latest["observation_id"],
                             element_id=self.chosen(latest, "Fixture cart"))
        self.assertEqual(captured["status"], "captured_private")
        self.assertTrue(Path(captured["path"]).is_file())

    def test_search_delay_and_login_during_delay_return_actual_page_not_success_claim(self):
        self.call("open")
        searched = self.call("search", query="Agent chosen", wait_ms=1500)
        self.assertEqual(searched["status"], "done")
        self.assertIn("New results", searched["observation"]["text"])
        observation = searched["observation"]
        login = self.call("act", action="click", observation_id=observation["observation_id"],
                          element_id=self.chosen(observation, "Login fixture"), intent="open", wait_ms=1000)
        self.assertEqual(login["page_status"], "login_required")
        self.assertNotIn("PRIVATE LOGIN CONTENT", json.dumps(login))
        self.assertNotIn("observation", login)
        self.assertEqual(self.call("observe")["status"], "login_required")


if __name__ == "__main__":
    unittest.main()
