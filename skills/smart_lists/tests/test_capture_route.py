"""Owner button captures use the registered route and the authoritative store."""
import asyncio
import json

from test_smart_lists import FakeRequest, SkillCase


class CaptureTests(SkillCase):
    def capture(self, body):
        route = self.api.routes["add"]
        self.assertEqual(route["methods"], ("POST",))
        response = asyncio.run(route["handler"](FakeRequest(body, method="POST")))
        return response.status_code, json.loads(response.body)

    def test_default_inbox_preserves_text_and_replays_request(self):
        body = {"text": "Buy milk\nand eggs", "request_id": "owner-click-1"}
        status, first = self.capture(body)
        self.assertEqual(status, 200)
        self.assertEqual(first["notice"], "Added to Inbox")
        self.assertEqual(self.capture(body), (200, first))
        entries = self.ok("read", group="Inbox")["entries"]
        self.assertEqual([(row["id"], row["text"]) for row in entries],
                         [(first["entry_id"], body["text"])])
        self.assertEqual(self.capture({**body, "text": "different"})[0], 400)

    def test_existing_case_insensitive_inbox_and_explicit_nested_group(self):
        self.ok("group", action="create", name="INBOX")
        self.assertEqual(self.capture({"text": "one"})[1]["notice"], "Added to INBOX")
        self.assertEqual(self.capture({"text": "one"})[0], 200)
        self.assertEqual(len(self.ok("read", group="Inbox")["entries"]), 2)
        self.ok("group", action="create", name="Later", parent="Inbox")
        status, result = self.capture({"text": "two", "group": "Inbox / Later"})
        self.assertEqual(status, 200)
        self.assertEqual(result["notice"], "Added to INBOX / Later")
        self.assertEqual(self.capture({"text": "three", "group": "Missing"})[0], 400)

    def test_invalid_text_is_400_and_does_not_create_inbox(self):
        for body in ({}, {"text": ""}, {"text": "   "}, {"text": 42}, ["text"], ValueError("bad json")):
            status, result = self.capture(body)
            self.assertEqual(status, 400)
            self.assertFalse(result["ok"])
            self.assertTrue(result["error"])
        self.assertEqual(self.module._service().overview()["tree"], [])

    def test_missing_store_is_not_silently_initialized(self):
        self.api = self.fresh_api(self.state_dir / "missing")
        self.assertEqual(self.capture({"text": "one"})[0], 400)
        self.assertFalse((self.state_dir / "missing" / "store.json").exists())
