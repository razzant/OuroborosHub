"""The chip calls the shared query engine; no self-HTTP or inferred money."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

from test_plugin import FakeAPI, FakeIndex, OwnedTemporaryCase, plugin, request


class UsageChipTests(OwnedTemporaryCase):
    def test_registered_route_queries_existing_24h_summary(self):
        api = FakeAPI(self.root)
        with patch.object(plugin, "UsageIndex", FakeIndex):
            service = plugin.register(api)
        self.addCleanup(service.close)
        payload = {"summary": {"physical_calls": 3, "reported_tokens": 1200000,
                              "fully_reported_rows": 2, "unknown_cost_rows": 1,
                              "metrics": {"cost_confirmed_usd": {"value": None}}},
                   "coverage": {"status": "complete", "issues": []}}
        query = AsyncMock(return_value=payload)
        with patch.object(service, "query", query):
            response = asyncio.run(api.routes["chip"][0](request()))
        query.assert_awaited_once_with({"period": "24h", "page_size": "1"})
        result = json.loads(response.body)
        self.assertEqual(result["text"], "24h · 1.2M tok")
        self.assertEqual(result["tone"], "muted")
        self.assertIn("24h physical calls: 3", result["title"])
        self.assertIn("Reported tokens: 1200000", result["title"])
        self.assertIn("Confirmed cost: unknown", result["title"])
        payload["summary"]["metrics"]["cost_confirmed_usd"]["value"] = 3.4
        self.assertEqual(plugin.usage_chip(payload)["text"], "$3.40 · 24h")
        payload["summary"]["metrics"]["cost_confirmed_usd"]["value"] = None
        payload["summary"]["reported_tokens"] = None
        self.assertEqual(plugin.usage_chip(payload)["text"], "24h · unknown tok")
        for status in ("loading", "error"):
            payload["coverage"] = {"status": status, "issues": ["Reading unavailable"]}
            self.assertEqual(plugin.usage_chip(payload), {"text": "—", "tone": "muted", "title": "Reading unavailable"})
        with patch.object(service, "query", AsyncMock(side_effect=OSError("private path"))):
            response = asyncio.run(api.routes["chip"][0](request()))
        self.assertEqual(json.loads(response.body), {"text": "—", "tone": "muted", "title": "Usage unavailable (OSError)"})
