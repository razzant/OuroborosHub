"""Offline WHOOP response-to-tool tests; no tokens, health data or network."""

import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


SKILL = Path(__file__).resolve().parents[1] / "skills" / "whoop-health"


def load_plugin():
    spec = importlib.util.spec_from_file_location("whoop_test_plugin", SKILL / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    # plugin.py imports/reloads its sibling by name: isolate both import effects.
    with patch.dict(sys.modules), patch.object(sys, "path", list(sys.path)):
        sys.modules.pop("whoop_client", None)
        spec.loader.exec_module(module)
    return module


class API:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, **spec):
        self.tools[name] = spec

    def get_settings(self, keys):
        return {key: "synthetic" for key in keys}

    def get_state_dir(self):
        return "unused-whoop-test-state"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.client = self.plugin.whoop_client
        self.api = API()
        self.plugin.register(self.api)
        self.auth = patch.object(self.client, "get_access_token", return_value="synthetic")
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.calls = []
        self.pages = {}
        self.http = patch.object(self.client.urllib.request, "urlopen", side_effect=self.respond)
        self.http.start()
        self.addCleanup(self.http.stop)

    def respond(self, request, timeout):
        url = urlsplit(request.full_url)
        self.assertEqual(url.hostname, "api.prod.whoop.com")
        self.assertEqual(timeout, 30)
        query = parse_qs(url.query)
        path = url.path.removeprefix("/developer/v2")
        token = query.get("nextToken", [""])[0]
        self.calls.append((path, token))
        return io.BytesIO(json.dumps(self.pages.get((path, token), {"records": []})).encode())

    def invoke(self, name, **args):
        data = json.loads(self.api.tools[name]["handler"](**args))
        self.assertEqual(data["status"], "ok", data)
        return data

    def test_zone_boundaries(self):
        for value, expected in [(0, "red"), (1, "red"), (33, "red"),
                                (34, "yellow"), (66, "yellow"),
                                (67, "green"), (100, "green"), (44.0, "yellow"),
                                (33.5, "yellow"), (66.5, "green")]:
            with self.subTest(value=value):
                self.assertEqual(self.client.recovery_zone(value), expected)

    def test_invalid_values_have_no_zone(self):
        for value in [None, True, False, "67", "", {}, [], -1, 101,
                      float("nan"), float("inf"), float("-inf")]:
            with self.subTest(value=repr(value)):
                self.assertIsNone(self.client.recovery_zone(value))

    def test_registered_fetch_enriches_each_page_preserving_data(self):
        self.pages = {
            ("/recovery", ""): {"records": [
                {"cycle_id": 1, "score_state": "SCORED", "score": {
                    "recovery_score": 33, "resting_heart_rate": 60, "user_calibrating": False}},
                {"cycle_id": 2, "score_state": "PENDING_SCORE", "score": None}],
                "next_token": "page-two"},
            ("/recovery", "page-two"): {"records": [
                {"cycle_id": 3, "score": {"recovery_score": 67}},
                {"cycle_id": 4, "score_state": "UNSCORABLE"}]},
        }
        result = self.invoke("whoop_fetch", data_type="RECOVERY", days_back="7")
        self.assertEqual(self.calls, [("/recovery", ""), ("/recovery", "page-two")])
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["records"][0]["score"], {
            "recovery_score": 33, "resting_heart_rate": 60,
            "user_calibrating": False, "recovery_zone": "red"})
        self.assertIsNone(result["records"][1]["score"])
        self.assertEqual(result["records"][2]["score"]["recovery_zone"], "green")
        self.assertNotIn("score", result["records"][3])

    def test_fetch_and_summary_agree_at_each_boundary(self):
        for value, expected in [(0, "red"), (33, "red"), (34, "yellow"),
                                (66, "yellow"), (67, "green"), (100, "green"),
                                (None, None), (-1, None), (101, None)]:
            with self.subTest(value=value):
                self.pages = {("/recovery", ""): {"records": [{"score": {
                    "recovery_score": value, "hrv_rmssd_milli": 31.5}}]}}
                fetched = self.invoke("whoop_fetch", data_type="recovery")
                summary = self.invoke("whoop_summary")
                self.assertEqual(fetched["records"][0]["score"]["recovery_zone"], expected)
                self.assertEqual(summary["recovery"]["recovery_zone"], expected)
                self.assertEqual(summary["recovery"]["recovery_score"], value)
                self.assertEqual(summary["recovery"]["hrv_rmssd_ms"], 31.5)

    def test_null_scores_do_not_break_summary_or_borrow_older_recovery(self):
        for state in ["PENDING_SCORE", "UNSCORABLE"]:
            with self.subTest(state=state):
                self.pages = { (path, ""): {"records": [{"score_state": state, "score": None}]}
                               for path in ["/recovery", "/activity/sleep", "/cycle"] }
                self.pages[("/recovery", "")]["records"].append({
                    "score_state": "SCORED", "score": {"recovery_score": 99}})
                result = self.invoke("whoop_summary")
                self.assertTrue(all(value is None for value in result["recovery"].values()))
                self.assertTrue(all(value is None for value in result["sleep"].values()))
                self.assertTrue(all(value is None for value in result["strain"].values()))

    def test_summary_preserves_calibration_flag(self):
        for flag in [True, False, None]:
            with self.subTest(flag=flag):
                self.pages = {("/recovery", ""): {"records": [{"score": {
                    "recovery_score": 80, "user_calibrating": flag}}]}}
                result = self.invoke("whoop_summary")["recovery"]
                self.assertIs(result["user_calibrating"], flag)
                self.assertEqual(result["recovery_zone"], "green")

    def test_missing_scores_and_empty_collections(self):
        self.assertEqual(self.invoke("whoop_summary"), {"status": "ok", "source": "whoop", "days": 1})
        self.pages = {("/recovery", ""): {"records": [{"score_state": "PENDING_SCORE"}]}}
        self.assertIsNone(self.invoke("whoop_summary")["recovery"]["recovery_zone"])

    def test_other_fetch_types_do_not_gain_recovery_zone(self):
        for kind, path in [("SLEEP", "/activity/sleep"), ("WORKOUT", "/activity/workout"),
                           ("CYCLE", "/cycle")]:
            with self.subTest(kind=kind):
                records = [{"score": {"strain": 5.2}}, {"score": None}]
                self.pages = {(path, ""): {"records": records}}
                self.assertEqual(self.invoke("whoop_fetch", data_type=kind)["records"], records)

    def test_registration_keeps_five_tools_and_recovery_contract(self):
        self.assertEqual(set(self.api.tools), {
            "whoop_auth_url", "whoop_exchange_code", "whoop_check", "whoop_fetch", "whoop_summary"})
        self.assertEqual(self.api.tools["whoop_fetch"]["schema"]["required"], ["data_type"])
        for name in ["whoop_fetch", "whoop_summary"]:
            self.assertIn("recovery_zone", self.api.tools[name]["description"])
            self.assertEqual(self.api.tools[name]["timeout_sec"], 90)


if __name__ == "__main__":
    unittest.main()
