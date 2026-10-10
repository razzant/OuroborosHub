"""Keenable route persistence and tool contracts, without a host or vendor call."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from skills.keenable import plugin


SEARCH = {
    "ok": True,
    "query": "Primary sources",
    "results": [{"title": "Example source", "url": "https://example.org/source",
                 "snippet": "A synthetic source, with Unicode: источники."}],
    "count": 1,
    "parse_status": "records",
    "filters_requested": {},
    "filter_observations": {},
    "index_freshness": {"newest_acquired_observed": "2026-10-04"},
    "raw": "Title: Example source\nURL: https://example.org/source",
}
PAGE = {
    "ok": True,
    "url": "https://example.org/source",
    "title": "Example source",
    "content": "# Example source\n\nFull synthetic page text.",
    "extraction_mode": "full_page",
    "cache": {"live": False, "snapshot_date": None},
    "served": {"served_url": "https://example.org/source"},
    "content_chars_total": 48,
    "content_truncated_by_skill": False,
    "vendor_content_complete": None,
    "content_incompleteness_indicators": [],
}
FAILURE = {
    "ok": False,
    "error": "keenable_rate_limited",
    "error_class": "not_read",
    "message": "Synthetic vendor quota reached.",
    "retryable": True,
    "raw": "Synthetic rate-limit response",
}


class Request:
    def __init__(self, payload=None, method="GET", json_error=None):
        self.payload = payload
        self.method = method
        self.json_error = json_error
        self.app = SimpleNamespace(state=SimpleNamespace(repo_dir=Path("/synthetic/host")))

    async def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class PluginAPI:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.tools = {}
        self.routes = {}
        self.tabs = {}
        self.unload = None

    def get_settings(self, keys):
        return {"KEENABLE_API_KEY": "synthetic-key"}

    def get_state_dir(self):
        return self.state_dir

    def register_tool(self, name, **spec):
        self.tools[name] = spec

    def register_route(self, name, **spec):
        self.routes[name] = spec

    def register_ui_tab(self, name, **spec):
        self.tabs[name] = spec

    def on_unload(self, callback):
        self.unload = callback


def body(response):
    if isinstance(response, dict):
        return response
    return json.loads(response.body.decode("utf-8"))


class KeenableWidgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="keenable-widget-test-")
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)
        self.api = PluginAPI(self.state_dir)
        self.addCleanup(plugin._on_unload)
        plugin.register(self.api)

    def route(self, name, payload=None, method="POST", **kwargs):
        return body(asyncio.run(self.api.routes[name]["handler"](
            Request(payload, method, **kwargs))))

    def state(self):
        return self.route("state", method="GET")

    def search(self, result=SEARCH):
        with patch.object(plugin, "client_search", return_value=copy.deepcopy(result)):
            return self.route("search", {"query": "Primary sources", "mode": "realtime"})

    def fetch(self, result=PAGE, **extra):
        request = {"url": PAGE["url"], **extra}
        with patch.object(plugin, "client_fetch", return_value=copy.deepcopy(result)):
            return self.route("fetch", request)

    def test_registration_keeps_tools_and_routes_and_uses_auto_sized_module(self):
        self.assertEqual(set(self.api.tools), {"search_web_pages", "fetch_page_content"})
        for name in ("search", "fetch"):
            self.assertEqual(tuple(self.api.routes[name]["methods"]), ("POST",))
        self.assertEqual(set(self.api.routes["state"]["methods"]), {"GET", "POST"})
        self.assertEqual(tuple(self.api.routes["author-kit"]["methods"]), ("GET",))
        self.assertEqual(self.api.tabs["keenable"]["render"], {
            "kind": "module", "entry": "widget.js", "start": "auto",
            "appearance": "host", "span": 2,
        })
        self.assertEqual(self.api.tools["search_web_pages"]["schema"]["properties"]
                         ["mode"]["enum"], ["pro", "realtime"])
        self.assertIs(self.api.unload, plugin._on_unload)

    def test_empty_state_does_not_invent_results_or_create_files(self):
        restored = self.state()
        self.assertEqual(restored["draft"], {})
        self.assertEqual(restored["results"], {})
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_search_route_preserves_envelope_and_persists_request_and_result(self):
        request = {"query": "Primary sources", "site": "example.org", "mode": "realtime"}
        envelope = copy.deepcopy(SEARCH)
        with patch.object(plugin, "client_search", return_value=envelope) as search:
            response = self.route("search", request)
        search.assert_called_once_with(request, "synthetic-key")
        self.assertEqual({key: response[key] for key in SEARCH}, SEARCH)
        self.assertEqual(envelope, SEARCH)
        restored = self.state()["results"]["search"]
        self.assertEqual(restored["request"], request)
        self.assertEqual(restored["result"], response)
        self.assertEqual(self.state()["draft"], {})

    def test_page_and_question_answers_survive_independently(self):
        search = self.search()
        page = self.fetch()
        answer = {**PAGE, "content": "A synthetic targeted answer.",
                  "extraction_mode": "prompt_extraction"}
        question = "Which source is mentioned?"
        response = self.fetch(answer, prompt=question)
        results = self.state()["results"]
        self.assertEqual(results["search"]["result"], search)
        self.assertEqual(results["page"]["result"], page)
        self.assertEqual(results["answer"]["result"], response)
        self.assertEqual(results["answer"]["request"]["prompt"], question)
        self.assertNotIn("prompt", results["page"]["request"])

    def test_draft_save_cannot_replace_results(self):
        search = self.search()
        draft = {"view": "read", "query": "New query", "url": PAGE["url"],
                 "live": True, "filter_open": True, "max_chars": 12000}
        self.route("state", {"draft": draft, "results": {"search": "forged"}})
        restored = self.state()
        self.assertEqual(restored["draft"], draft)
        self.assertEqual(restored["results"]["search"]["result"], search)
        self.fetch()
        self.assertEqual(self.state()["draft"], draft)

    def test_tools_return_unmodified_client_envelopes_and_do_not_save_widget_state(self):
        for name, helper, result, request in (
            ("search_web_pages", "client_search", SEARCH, {"query": "Primary sources"}),
            ("fetch_page_content", "client_fetch", PAGE, {"url": PAGE["url"]}),
            ("search_web_pages", "client_search", FAILURE, {"query": "Primary sources"}),
        ):
            with self.subTest(tool=name, ok=result["ok"]):
                value = copy.deepcopy(result)
                with patch.object(plugin, helper, return_value=value) as client:
                    returned = self.api.tools[name]["handler"](**request)
                self.assertEqual(json.loads(returned), result)
                self.assertEqual(value, result)
                args, kwargs = client.call_args
                self.assertEqual(args, (request, "synthetic-key"))
                self.assertEqual(kwargs, {"content_char_limit": plugin.CONTENT_CHAR_LIMIT}
                                 if helper == "client_fetch" else {})
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_author_kit_is_read_at_request_time_and_returns_css_only(self):
        assets = {"css": ".ouro-ui { color: inherit; }", "javascript": "not needed"}
        read_assets = Mock(return_value=assets)
        helper = SimpleNamespace(read_author_kit_assets=read_assets)
        with patch.dict(sys.modules, {"ouroboros.server_web": helper}):
            for _ in range(2):
                response = self.route("author-kit", method="GET")
                self.assertEqual(response["css"], assets["css"])
                self.assertNotIn("javascript", response)
        self.assertEqual(read_assets.call_count, 2)
        read_assets.assert_called_with(Path("/synthetic/host"))

    def test_missing_author_kit_does_not_disable_tools(self):
        with patch.dict(sys.modules, {"ouroboros.server_web": None}):
            response = self.route("author-kit", method="GET")
        self.assertFalse(response["ok"])
        self.assertTrue(response.get("message") or response.get("error"))
        with patch.object(plugin, "client_search", return_value=SEARCH):
            self.assertEqual(json.loads(plugin._tool_search(query="Primary sources")), SEARCH)

    def test_vendor_calls_run_off_the_server_event_loop(self):
        caller_thread = threading.get_ident()
        worker_threads = []

        def search(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return copy.deepcopy(SEARCH)

        def fetch(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return copy.deepcopy(PAGE)

        with patch.object(plugin, "client_search", side_effect=search), \
                patch.object(plugin, "client_fetch", side_effect=fetch):
            self.route("search", {"query": "Primary sources"})
            self.route("fetch", {"url": PAGE["url"]})
        self.assertEqual(len(worker_threads), 2)
        self.assertTrue(all(thread != caller_thread for thread in worker_threads))

    def test_worker_keeps_result_after_widget_request_is_cancelled(self):
        started = threading.Event()
        release = threading.Event()
        draft = {"view": "search", "query": "A newer unsent draft"}

        def search(*args, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("The test did not release its vendor worker")
            return copy.deepcopy(SEARCH)

        async def disconnect_widget():
            task = asyncio.create_task(self.api.routes["search"]["handler"](
                Request({"query": "Primary sources"}, "POST")))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                await self.api.routes["state"]["handler"](
                    Request({"draft": draft}, "POST"))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()

        with patch.object(plugin, "client_search", side_effect=search):
            # asyncio.run joins its default executor after the route is cancelled,
            # so the provider worker has finished before persistence is inspected.
            asyncio.run(disconnect_widget())
        restored = self.state()
        self.assertEqual(restored["draft"], draft)
        slot = restored["results"]["search"]
        self.assertEqual(slot["request"], {"query": "Primary sources"})
        self.assertEqual({key: slot["result"][key] for key in SEARCH}, SEARCH)

    def test_known_draft_values_roundtrip_without_clamping_tool_options(self):
        draft = {
            "view": "read", "query": "Original papers about cognition",
            "site": "example.org", "published_after": "2026-01-01",
            "published_before": "2026-10-04", "acquired_after": "2026-02-01",
            "acquired_before": "2026-10-04", "mode": "realtime",
            "url": PAGE["url"], "question": "What was observed?", "live": False,
            "max_chars": plugin.CONTENT_CHAR_LIMIT * 2,
            "filter_open": True, "read_advanced": False,
        }
        response = self.route("state", {"draft": draft})
        self.assertTrue(response["ok"], response)
        self.assertEqual(self.state()["draft"], draft)

    def test_invalid_draft_does_not_destroy_previous_saved_values(self):
        draft = {"query": "Keep this query", "view": "search", "live": False}
        self.route("state", {"draft": draft})
        invalid = [
            None, [], {"query": ["not", "text"]}, {"question": {"nested": "value"}},
            {"live": "false"}, {"filter_open": 1}, {"read_advanced": None},
            {"view": "history"}, {"mode": "invented"}, {"max_chars": {}},
            {"max_chars": True}, {"query": "q" * 1_000_000}, {"unknown": "not a draft"},
        ]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                response = self.route("state", {"draft": value})
                self.assertFalse(response["ok"], response)
                self.assertTrue(response.get("message") or response.get("error"))
                self.assertEqual(self.state()["draft"], draft)
        response = self.route("state", json_error=ValueError("invalid JSON"))
        self.assertFalse(response["ok"], response)
        self.assertEqual(self.state()["draft"], draft)

    def test_failure_guidance_keeps_typed_vendor_failure_and_raw_evidence(self):
        for route in ("search", "fetch"):
            with self.subTest(route=route):
                response = self.search(FAILURE) if route == "search" else self.fetch(FAILURE)
                self.assertEqual({key: response[key] for key in FAILURE}, FAILURE)
                self.assertTrue(response["ui_guidance"].strip())
                self.assertNotIn(FAILURE["message"], response["ui_guidance"])
                self.assertEqual(response["message"], FAILURE["message"])
                self.assertIsInstance(response["ui_notes"], list)
                slot = "search" if route == "search" else "page"
                self.assertEqual(self.state()["results"][slot]["result"], response)

    def test_draft_write_is_atomic_and_failed_replace_preserves_prior_file(self):
        first = {"query": "Saved before failure", "view": "search"}
        second = {"query": "New complete value", "view": "read"}
        self.assertTrue(self.route("state", {"draft": first})["ok"])
        target = self.state_dir / "widget-draft.json"
        before = target.read_bytes()
        original_replace = os.replace
        replacements = []

        def check_atomic_replace(source, destination):
            source, destination = Path(source), Path(destination)
            self.assertEqual(destination, target)
            self.assertEqual(source.parent, target.parent)
            self.assertNotEqual(source, target)
            self.assertEqual(target.read_bytes(), before)
            self.assertIn("New complete value", source.read_text(encoding="utf-8"))
            replacements.append((source, destination))
            original_replace(source, destination)

        with patch.object(os, "replace", side_effect=check_atomic_replace):
            self.assertTrue(self.route("state", {"draft": second})["ok"])
        self.assertEqual(len(replacements), 1)
        self.assertEqual(self.state()["draft"], second)
        before_failure = target.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("Synthetic disk error")):
            response = self.route("state", {"draft": first})
        self.assertFalse(response["ok"], response)
        self.assertEqual(target.read_bytes(), before_failure)
        self.assertEqual(self.state()["draft"], second)
        self.assertEqual(list(self.state_dir.iterdir()), [target])

    def test_result_save_failure_returns_vendor_result_with_visible_disclosure(self):
        self.search()
        previous = (self.state_dir / "widget-search.json").read_bytes()
        next_result = {**SEARCH, "query": "Another search", "count": 0, "results": []}
        with patch.object(os, "replace", side_effect=OSError("Synthetic disk error")):
            response = self.search(next_result)
        self.assertTrue(response["ok"], response)
        self.assertEqual({key: response[key] for key in next_result}, next_result)
        self.assertTrue(response.get("ui_state_warning"), response)
        self.assertEqual((self.state_dir / "widget-search.json").read_bytes(), previous)
        self.assertEqual([path.name for path in self.state_dir.iterdir()], ["widget-search.json"])

    def test_corrupt_saved_state_is_disclosed_and_not_overwritten(self):
        path = self.state_dir / "widget-draft.json"
        path.write_text("{incomplete JSON", encoding="utf-8")
        response = self.state()
        self.assertFalse(response["ok"], response)
        self.assertTrue(response["message"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{incomplete JSON")
        # Restoration failure cannot make a new vendor request unusable.
        self.assertTrue(self.search()["ok"])

    def test_material_retrieval_observations_get_notes_without_losing_evidence(self):
        search = {
            **SEARCH, "parse_status": "partial", "results_omitted": 2,
            "filter_observations": {"published_after": {"conclusion": "observed_conflict"}},
        }
        page = {
            **PAGE, "content_truncated_by_skill": True, "max_chars_clamped": True,
            "max_chars_effective": 12000,
            "content_incompleteness_indicators": ["served_url_differs_from_request"],
        }
        for envelope, call, expected_facts in (
            (search, self.search, ("parsed", "additional results", "dates")),
            (page, self.fetch, ("limit", "served URL", "12000")),
        ):
            with self.subTest(kind="search" if call == self.search else "page"):
                response = call(envelope)
                self.assertEqual({key: response[key] for key in envelope}, envelope)
                notes = " ".join(response["ui_notes"])
                for fact in expected_facts:
                    self.assertIn(fact, notes)


if __name__ == "__main__":
    unittest.main()
