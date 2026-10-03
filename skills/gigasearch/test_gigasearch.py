from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("plugin.py")
SPEC = importlib.util.spec_from_file_location("gigasearch_plugin", MODULE_PATH)
plugin = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(plugin)


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int):
        return self.payload


class GigaSearchTests(unittest.TestCase):
    def test_normalises_results_dates_and_model_summary(self):
        payload = {
            "answer": "Пересказ ответа",
            "data": {
                "items": [
                    {
                        "name": "Источник",
                        "link": "https://example.test/article",
                        "content": "Фрагмент",
                        "published_date": "2026-09-30",
                    }
                ]
            },
        }
        result = plugin._normalise_response("запрос", payload)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary_kind"], "model_generated")
        self.assertEqual(result["results"][0]["published_at"], "2026-09-30")
        self.assertEqual(result["sources"][0]["url"], "https://example.test/article")

    def test_empty_results_are_not_an_error(self):
        result = plugin._normalise_response("ничего", {"results": []})
        self.assertEqual(result, {"ok": True, "status": "empty", "query": "ничего", "results": [], "count": 0})

    def test_recognised_empty_shapes_remain_successful(self):
        for payload in ([], {"data": {"items": []}}):
            with self.subTest(payload=payload):
                result = plugin._normalise_response("query", payload)
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "empty")

    def test_empty_alias_does_not_hide_other_supported_results(self):
        result = plugin._normalise_response("query", {
            "results": [], "data": {"items": [{"url": "https://example.test/hit"}]},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["url"], "https://example.test/hit")

    def test_unknown_and_malformed_responses_are_errors_not_empty(self):
        for payload in ({}, {"detail": "invalid request"}, {"results": "invalid"},
                        {"data": {"error": "unavailable"}},
                        {"results": [{"title": "Missing URL"}]},
                        {"results": [{"url": "https://example.test"}, None]}):
            with self.subTest(payload=payload):
                result = plugin._normalise_response("query", payload)
                self.assertIs(result["ok"], False)
                self.assertEqual(result["status"], "error")

    def test_service_error_is_not_reported_as_empty(self):
        result = plugin._normalise_response("запрос", {"status": "error", "message": "quota exceeded"})
        self.assertEqual(result["status"], "error")
        self.assertIs(result["ok"], False)
        self.assertIn("quota exceeded", result["error"])

    def test_citations_are_preserved_for_model_summary(self):
        result = plugin._normalise_response(
            "запрос",
            {
                "summary": "Пересказ",
                "citations": [{"title": "Источник", "url": "https://example.test/source"}],
            },
        )
        self.assertEqual(result["summary_kind"], "model_generated")
        self.assertEqual(result["sources"][0]["url"], "https://example.test/source")

    def test_explicit_citations_survive_separate_or_empty_results(self):
        for results in ([], [{"url": "https://example.test/hit"}]):
            with self.subTest(results=results):
                result = plugin._normalise_response("query", {
                    "summary": "Summary", "results": results,
                    "citations": [{"title": "Citation", "url": "https://example.test/cited"}],
                })
                self.assertTrue(result["ok"])
                self.assertEqual(result["count"], len(results))
                self.assertEqual(result["sources"], [
                    {"title": "Citation", "url": "https://example.test/cited"}])

    def test_preserves_both_reference_fields_and_nested_references(self):
        result = plugin._normalise_response("query", {
            "answer": "Summary", "results": [],
            "sources": [{"url": "https://example.test/source"}],
            "data": {"citations": [{"url": "https://example.test/citation"}]},
        })
        self.assertEqual([item["url"] for item in result["sources"]],
                         ["https://example.test/source", "https://example.test/citation"])

    def test_references_without_a_summary_are_not_reported_empty(self):
        result = plugin._normalise_response("query", {
            "results": [], "citations": [{"url": "https://example.test/source"}],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sources"][0]["url"], "https://example.test/source")

    def test_explicit_empty_citations_are_not_replaced_with_search_hits(self):
        result = plugin._normalise_response("query", {
            "answer": "Summary", "results": [{"url": "https://example.test/hit"}],
            "citations": [],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["sources"], [])

    def test_reference_only_response_keeps_record_details(self):
        result = plugin._normalise_response("query", {
            "sources": {"items": [{"url": "https://example.test/source", "snippet": "Evidence"}]},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["snippet"], "Evidence")
        self.assertEqual(result["sources"][0]["url"], "https://example.test/source")

    def test_answer_without_results_remains_a_summary(self):
        result = plugin._normalise_response("query", {"answer": "Summary"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary_kind"], "model_generated")
        self.assertEqual(result["sources"], [])

    def test_invalid_explicit_references_are_reported(self):
        for references in ("invalid", [{"title": "Missing URL"}]):
            with self.subTest(references=references):
                result = plugin._normalise_response("query", {
                    "answer": "Summary", "results": [], "citations": references,
                })
                self.assertIs(result["ok"], False)

    @patch.object(plugin.urllib.request, "urlopen")
    def test_posts_configured_shape_and_bearer_key(self, urlopen):
        urlopen.return_value = _Response(
            {"results": [{"title": "T", "url": "https://example.test", "snippet": "S"}]}
        )
        result = plugin._search("test", 3, "https://search.example.test/v1/search", "secret")
        self.assertEqual(result["status"], "ok")
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"query": "test", "limit": 3})
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    @patch.object(plugin.urllib.request, "urlopen")
    def test_http_failure_has_the_host_error_flag(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example.test/search", 401, "Unauthorized", {}, None)
        result = plugin._search("query", 5, "https://example.test/search", "test-key")
        self.assertIs(result["ok"], False)
        self.assertEqual(result["status"], "error")
        self.assertIn("401", result["error"])

    def test_registers_preferred_search_description(self):
        class API:
            def __init__(self):
                self.tool = None

            def register_tool(self, **tool):
                self.tool = tool

            def log(self, *_args):
                pass

            def get_settings(self, keys):
                self.requested_settings = keys
                return {"GIGASEARCH_API_URL": "https://example.test/search",
                        "GIGASEARCH_API_KEY": "test-key"}

        api = API()
        plugin.register(api)
        required = (
            "Основной веб-поиск этой установки. Для обычного поиска внешней "
            "информации сначала используй GigaSearch."
        )
        self.assertEqual(api.tool["name"], "gigasearch_search")
        self.assertTrue(api.tool["description"].startswith(required))
        self.assertLessEqual(len(required), 120)
        with patch.object(plugin, "_search", return_value={"ok": True, "status": "empty"}) as search:
            result = json.loads(api.tool["handler"](query="query", limit=3))
            self.assertTrue(result["ok"])
            search.assert_called_once_with("query", 3, "https://example.test/search", "test-key")
            self.assertEqual(api.requested_settings, list(plugin._SETTINGS))

    def test_configuration_errors_are_explicit(self):
        with patch.object(plugin.urllib.request, "urlopen", return_value=_Response({"results": []})):
            self.assertEqual(plugin._search("q", 5, "http://example.test", "key")["status"], "empty")
        self.assertEqual(plugin._search("q", 5, "ftp://example.test", "key")["status"], "error")
        self.assertEqual(plugin._search("q", 5, "https://example.test", "")["status"], "error")


if __name__ == "__main__":
    unittest.main()
