"""The registered DuckDuckGo HTTP route refuses a body that is not a JSON object before it searches."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.routing import Route


PATH = Path(__file__).resolve().parents[1] / "skills/duckduckgo/plugin.py"
spec = importlib.util.spec_from_file_location("duckduckgo_under_test", PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class API:
    def __init__(self):
        self.routes = {}

    def register_route(self, name, handler, **kwargs):
        self.routes[name] = (handler, kwargs["methods"])

    def register_tool(self, *_args, **_kwargs):
        pass

    def register_ui_tab(self, *_args, **_kwargs):
        pass

    def log(self, *_args):
        pass


class DuckDuckGoRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = API()
        plugin.register(self.api)
        handler, methods = self.api.routes["search"]
        app = Starlette(routes=[Route("/search", handler, methods=methods)])
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture")
        self.addAsyncCleanup(self.client.aclose)
        self.searched = []

        def search(query, max_results):
            self.searched.append((query, max_results))
            return {"results": [{"title": "Fixture", "url": "https://example.invalid/", "snippet": "fixture"}]}

        mock = patch.object(plugin, "_search", side_effect=search)
        mock.start()
        self.addCleanup(mock.stop)

    async def test_non_object_json_is_400_before_search(self):
        for value in (None, [], ["query"], "text", 3, True):
            with self.subTest(value=value):
                response = await self.client.post("/search", content=json.dumps(value),
                                                  headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("JSON object", response.json()["error"])
        self.assertEqual(self.searched, [])

    async def test_invalid_json_keeps_existing_400(self):
        response = await self.client.post("/search", content="{broken")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid JSON body")
        self.assertEqual(self.searched, [])

    async def test_non_integer_max_results_is_400_before_search(self):
        for literal in ("1e400", "-1e400", "Infinity", "-Infinity", "NaN", "true", "false", "3.9", "[]", "{}"):
            with self.subTest(literal=literal):
                response = await self.client.post("/search", content='{"query": "x", "max_results": %s}' % literal,
                                                  headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "max_results must be an integer")
        self.assertEqual(self.searched, [])

    async def test_missing_blank_non_string_or_long_query_is_400_before_search(self):
        for body in ({}, {"query": None}, {"query": "   "}, {"query": 42}, {"query": ["x"]}):
            with self.subTest(body=body):
                response = await self.client.post("/search", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "query is required")
        response = await self.client.post("/search", json={"query": "x" * 401})
        self.assertEqual(response.status_code, 400)
        self.assertIn("too long", response.json()["error"])
        self.assertEqual(self.searched, [])

    async def test_object_body_reaches_search(self):
        response = await self.client.post("/search", json={"query": " ouroboros ", "max_results": "3"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["title"], "Fixture")
        response = await self.client.post("/search", json={"query": "x" * 400})
        self.assertEqual(response.status_code, 200)
        for fields in ({"max_results": 4}, {"max_results": 7.0}, {"max_results": " 12 "}, {"max_results": ""},
                       {"max_results": None}):
            with self.subTest(fields=fields):
                response = await self.client.post("/search", json={"query": "q", **fields})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.searched, [("ouroboros", 3), ("x" * 400, 5), ("q", 4), ("q", 7), ("q", 12), ("q", 5),
                                         ("q", 5)])


class DuckDuckGoToolTests(unittest.TestCase):
    def test_tool_path_reads_a_non_finite_max_results_as_the_default(self):
        seen = []

        class FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def text(self, query, max_results):
                seen.append((query, max_results))
                return [{"title": "Fixture", "href": "https://example.invalid/", "body": "fixture"}]

        with patch.dict(sys.modules, {"ddgs": types.SimpleNamespace(DDGS=FakeDDGS)}):
            for value, expected in ((float("inf"), 5), (float("-inf"), 5), (3, 3), (99, 20)):
                with self.subTest(value=value):
                    payload = json.loads(plugin._tool_search(query="x", max_results=value))
                    self.assertEqual(payload["count"], 1)
                    self.assertEqual(seen[-1], ("x", expected))


if __name__ == "__main__":
    unittest.main()
