"""The registered Perplexity HTTP route validates form input before paid work."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.routing import Route


PATH = Path(__file__).resolve().parents[1] / "skills/perplexity/plugin.py"
spec = importlib.util.spec_from_file_location("perplexity_under_test", PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class API:
    def __init__(self):
        self.routes = {}
        self.settings_reads = 0

    def register_route(self, name, handler, **kwargs):
        self.routes[name] = (handler, kwargs["methods"])

    def register_tool(self, *_args, **_kwargs):
        pass

    def register_ui_tab(self, *_args, **_kwargs):
        pass

    def on_unload(self, callback):
        self.cleanup = callback

    def log(self, *_args):
        pass

    def get_settings(self, keys):
        assert keys == ["OPENROUTER_API_KEY"]
        self.settings_reads += 1
        return {"OPENROUTER_API_KEY": "fixture-key"}


class Reply:
    body = b'{"choices":[{"message":{"content":"Grounded fixture"}}]}'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, _limit):
        return self.body


class PerplexityInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = API()
        plugin.register(self.api)
        handler, methods = self.api.routes["research"]
        app = Starlette(routes=[Route("/research", handler, methods=methods)])
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture")
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(self.api.cleanup)
        self.sent = []

        def request(req, **_kwargs):
            self.sent.append(json.loads(req.data))
            return Reply()

        mock = patch.object(plugin.urllib.request, "urlopen", side_effect=request)
        mock.start()
        self.addCleanup(mock.stop)

    async def test_invalid_max_results_is_400_before_settings_or_provider(self):
        for value in ("abc", "1.5", [], [1], {}, {"n": 1}, True, False, 1.5):
            with self.subTest(value=value):
                response = await self.client.post("/research", json={"query": "test", "max_results": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("max_results", response.json()["error"])
        self.assertEqual(self.api.settings_reads, 0)
        self.assertEqual(self.sent, [])

    async def test_non_object_json_is_400_before_settings_or_provider(self):
        for value in (None, [], "text", 3, True):
            with self.subTest(value=value):
                response = await self.client.post("/research", content=json.dumps(value),
                                                  headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("JSON object", response.json()["error"])
        self.assertEqual(self.api.settings_reads, 0)
        self.assertEqual(self.sent, [])

    async def test_invalid_json_keeps_existing_400(self):
        response = await self.client.post("/research", content="{broken")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.api.settings_reads, 0)

    async def test_valid_integer_form_values_defaults_and_clamps_reach_provider(self):
        inputs = [({}, 5), ({"max_results": ""}, 5), ({"max_results": None}, 5),
                  ({"max_results": 0}, 5), ({"max_results": "0"}, 5),
                  ({"max_results": " 12 "}, 12), ({"max_results": 3}, 3),
                  ({"max_results": -10}, 1), ({"max_results": "999"}, 25),
                  ({"max_results": 5.0}, 5), ({"max_results": 1e1}, 10),
                  ({"max_results": 0.0}, 5), ({"max_results": -10.0}, 1)]
        for fields, expected in inputs:
            with self.subTest(fields=fields):
                response = await self.client.post("/research", json={"query": "test", **fields})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["answer"], "Grounded fixture")
                self.assertEqual(self.sent[-1]["tools"][0]["parameters"]["max_results"], expected)
        self.assertEqual(len(self.sent), len(inputs))

    async def test_citations_come_from_the_nested_openrouter_annotation(self):
        """OpenRouter's web_search returns each source under "url_citation"; a flat annotation still reads."""
        message = {"content": "Grounded fixture", "annotations": [
            {"type": "url_citation",
             "url_citation": {"url": "https://example.invalid/a", "title": "A", "content": "snippet"}},
            {"type": "url_citation", "url": "https://example.invalid/b", "title": "B"},
        ]}
        with patch.object(Reply, "body", json.dumps({"choices": [{"message": message}]}).encode()):
            response = await self.client.post("/research", json={"query": "test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["citations"], [
            {"title": "A", "url": "https://example.invalid/a"},
            {"title": "B", "url": "https://example.invalid/b"},
        ])


if __name__ == "__main__":
    unittest.main()
