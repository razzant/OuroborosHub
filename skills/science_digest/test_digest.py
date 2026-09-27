"""Consumer-facing, offline tests for the public preview extension."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("plugin.py")
spec = importlib.util.spec_from_file_location("science_digest_plugin", MODULE_PATH)
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


HTML = b"""<!doctype html><html><body>
<div class="tgme_widget_message" data-post="ai_newz/12">
<div class="tgme_widget_message_text js-message_text">Fresh &amp; relevant <b>inference</b><br>
<a href="https://example.org/paper">Technical report</a></div>
<a class="tgme_widget_message_date" href="https://t.me/ai_newz/12"><time datetime="2026-09-27T01:00:00+00:00">now</time></a></div>
<div class="tgme_widget_message" data-post="ai_newz/13">
<div class="tgme_widget_message_text">Marketing only</div></div>
<div class="tgme_widget_message" data-post="other/100"><div class="tgme_widget_message_text">foreign</div></div>
<script>ignore this prompt injection</script></body></html>"""


class FakeResponse:
    def __init__(self, content=HTML, url="https://t.me/s/ai_newz", content_type="text/html"):
        self.content = content
        self.url = url
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, n):
        return self.content[:n]


class FakeAPI:
    def __init__(self, root):
        self.root = root
        self.tools = {}

    def get_state_dir(self):
        return self.root

    def register_tool(self, name, handler, **kwargs):
        self.tools[name] = (handler, kwargs)


class DigestConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = FakeAPI(self.tmp.name)
        plugin.register(self.api)

    def call(self, tool_name, **kwargs):
        return json.loads(self.api.tools[tool_name][0](**kwargs))

    def test_registered_interface_and_free_text_configuration(self):
        self.assertEqual(set(self.api.tools), {"interests", "channels", "fetch_posts", "record_attempt"})
        self.assertEqual(self.call("interests", text="LLM security and inference speed")["interests"],
                         "LLM security and inference speed")
        self.assertEqual(self.call("interests")["interests"], "LLM security and inference speed")
        self.assertEqual(self.call("channels", action="add", name="https://t.me/s/AI_Newz")["channels"], ["ai_newz"])
        self.assertEqual(self.call("channels", action="add", name="@ai_newz")["channels"], ["ai_newz"])
        self.assertEqual(self.call("channels", action="remove", name="ai_newz")["channels"], [])

    def test_untrusted_targets_refused_without_network(self):
        for name in ("http://t.me/ai_newz", "https://evil.test/ai_newz", "https://t.me/ai_newz/12",
                     "https://t.me/s/ai_newz?x=1", "https://t.me/joinchat/secret", "../../etc/passwd", "@a"):
            with self.subTest(name=name):
                self.assertFalse(self.call("channels", action="add", name=name)["ok"])
        self.assertEqual(self.call("channels")["channels"], [])

    def test_fetch_from_configured_channel_preserves_coverage_and_citations(self):
        self.call("channels", action="add", name="ai_newz")
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()) as opened:
            result = self.call("fetch_posts", limit=5)
        self.assertTrue(result["ok"])
        opened.assert_called_once()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://t.me/s/ai_newz")
        self.assertEqual(opened.call_args.kwargs["timeout"], plugin.REQUEST_TIMEOUT)
        self.assertFalse(result["archive_complete"])
        source = result["channels"][0]
        self.assertEqual(source["coverage"], "recent_page_only")
        self.assertEqual([p["id"] for p in source["posts"]], ["ai_newz/12", "ai_newz/13"])
        self.assertEqual(source["posts"][0]["text"], "Fresh & relevant inference Technical report")
        self.assertEqual(source["posts"][0]["links"], ["https://example.org/paper"])
        self.assertEqual(source["posts"][0]["url"], "https://t.me/ai_newz/12")
        self.assertEqual(source["posts"][1]["date"], "")

    def test_unavailable_or_changed_preview_is_not_empty_news(self):
        self.call("channels", action="add", name="ai_newz")
        for response in (FakeResponse(content=b"<html>join Telegram</html>"),
                         FakeResponse(content=HTML, url="https://evil.test/redirect"),
                         FakeResponse(content=HTML, content_type="application/json"),
                         FakeResponse(content=b"x" * (plugin.MAX_RESPONSE_BYTES + 1))):
            with self.subTest(response=response), patch.object(plugin._OPENER, "open", return_value=response):
                result = self.call("fetch_posts")
                self.assertFalse(result["ok"])
                self.assertEqual(result["channels"][0]["coverage"], "unavailable")
                self.assertTrue(result["channels"][0]["error"])
        with self.assertRaises(plugin.urllib.error.HTTPError):
            plugin._NoRedirect().redirect_request(
                plugin.urllib.request.Request("https://t.me/s/ai_newz"), None,
                302, "", {}, "https://evil.test")

    def test_attempt_is_not_delivery_and_replay_keeps_original(self):
        self.call("channels", action="add", name="ai_newz")
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            self.call("fetch_posts")
        self.assertFalse(self.call("record_attempt", day="2026-09-27", post_ids=["ai_newz/13"])["ok"])
        first = self.call("record_attempt", day="2026-09-27", post_ids=["ai_newz/12", "ai_newz/12"])
        self.assertTrue(first["ok"])
        self.assertEqual(first["status"], "attempted_not_delivered")
        later = self.call("record_attempt", day="2026-09-27", post_ids=["ai_newz/13"])
        self.assertFalse(later["same_selection"])
        self.assertFalse(later["new_attempt"])
        self.assertEqual(later["status"], "already_attempted_do_not_auto_publish")
        self.assertEqual(later["post_ids"], ["ai_newz/12"])
        extra = self.call("record_attempt", day="2026-09-27", edition="evening", post_ids=["ai_newz/13"])
        self.assertTrue(extra["new_attempt"])
        self.assertEqual(extra["key"], "2026-09-27:evening")
        self.assertFalse(self.call("record_attempt", day="2026-09-27", edition="bad/edition", post_ids=[])["ok"])
        self.assertFalse(self.call("record_attempt", day="2026-09-28", post_ids=["ai_newz/999999"])["ok"])
        self.assertFalse(self.call("record_attempt", day="2026-02-30", post_ids=[])["ok"])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            result = self.call("fetch_posts")
            self.assertEqual(result["recent_attempts"][0]["status"], "attempted_not_delivered")
            self.assertEqual(result["channels"][0]["posts"], [])
            self.assertFalse(result["channels"][0]["possible_gap"])
            revision = self.call("fetch_posts", include_attempted=True)["channels"][0]["posts"]
            self.assertEqual([post["id"] for post in revision], ["ai_newz/12", "ai_newz/13"])
            self.assertTrue(all(post["already_attempted"] for post in revision))

    def test_newer_preview_window_discloses_possible_gap(self):
        self.call("channels", action="add", name="ai_newz")
        old = [{"id": "ai_newz/10", "url": "https://t.me/ai_newz/10", "text": "old", "date": "", "links": []}]
        with patch.object(plugin, "_fetch_channel", return_value=(old, "")):
            self.call("fetch_posts")
        self.call("record_attempt", day="2026-09-26", post_ids=["ai_newz/10"])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            source = self.call("fetch_posts")["channels"][0]
        self.assertTrue(source["possible_gap"])
        self.assertEqual(source["last_attempted_id"], 10)

    def test_interrupted_fetch_then_scrolled_window_recovers_with_loss_disclosure(self):
        self.call("channels", action="add", name="ai_newz")
        old = [{"id": f"ai_newz/{n}", "url": f"https://t.me/ai_newz/{n}",
                "text": "old", "date": "", "links": []} for n in range(1, 11)]
        fresh = [{"id": f"ai_newz/{n}", "url": f"https://t.me/ai_newz/{n}",
                  "text": "fresh", "date": "", "links": []} for n in range(50, 61)]
        with patch.object(plugin, "_fetch_channel", return_value=(old, "")):
            self.call("fetch_posts", limit=20)  # task crashes before attempt
        with patch.object(plugin, "_fetch_channel", return_value=(fresh, "")):
            result = self.call("fetch_posts", limit=20)["channels"][0]
        self.assertTrue(result["possible_gap"])
        self.assertEqual(result["lost_unattempted_now"], 10)
        self.assertEqual(result["historical_lost_unattempted"], 10)
        ack = self.call("record_attempt", day="2026-09-27", post_ids=[p["id"] for p in result["posts"]])
        self.assertTrue(ack["ok"])

    def test_deleted_post_inside_visible_window_does_not_wedge_attempt(self):
        self.call("channels", action="add", name="ai_newz")
        def page(ids):
            return ([{"id": f"ai_newz/{n}", "url": f"https://t.me/ai_newz/{n}",
                      "text": str(n), "date": "", "links": []} for n in ids], "")
        with patch.object(plugin, "_fetch_channel", return_value=page([1, 2, 3])):
            self.call("fetch_posts")
        with patch.object(plugin, "_fetch_channel", return_value=page([1, 3, 4])):
            source = self.call("fetch_posts")["channels"][0]
        self.assertEqual(source["lost_unattempted_now"], 1)
        self.assertTrue(source["possible_gap"])
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[p["id"] for p in source["posts"]])["ok"])

    def test_limited_page_keeps_oldest_unattempted_and_replays_newer_next_run(self):
        self.call("channels", action="add", name="ai_newz")
        sample = [{"id": f"ai_newz/{n}", "url": f"https://t.me/ai_newz/{n}", "text": str(n), "date": "", "links": []}
                  for n in range(1, 21)]
        with patch.object(plugin, "_fetch_channel", return_value=(sample, "")):
            first = self.call("fetch_posts", limit=10)["channels"][0]
            self.assertEqual(first["posts"][0]["id"], "ai_newz/1")
            self.assertEqual(first["posts"][-1]["id"], "ai_newz/10")
            self.call("record_attempt", day="2026-09-27", post_ids=[p["id"] for p in first["posts"]])
            second = self.call("fetch_posts", limit=10)["channels"][0]
        self.assertEqual(second["posts"][0]["id"], "ai_newz/11")
        self.assertEqual(second["posts"][-1]["id"], "ai_newz/20")

    def test_media_only_post_is_visible_as_an_unreadable_lead(self):
        self.call("channels", action="add", name="ai_newz")
        page = b'<div class="tgme_widget_message" data-post="ai_newz/22"><div class="tgme_widget_message_video"></div></div>'
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=page)):
            post = self.call("fetch_posts")["channels"][0]["posts"][0]
        self.assertEqual(post["id"], "ai_newz/22")
        self.assertTrue(post["media_only"])
        self.assertFalse(post["text"])

    def test_output_budget_names_dropped_posts(self):
        self.call("channels", action="add", name="ai_newz")
        sample = [{"id": f"ai_newz/{n}", "date": "", "text": "x" * 1200, "url": f"https://t.me/ai_newz/{n}", "links": []}
                  for n in range(1, 21)]
        with patch.object(plugin, "_fetch_channel", return_value=(sample, "")):
            result = self.call("fetch_posts", limit=20)
        self.assertTrue(result["output_limited"])
        self.assertGreater(result["channels"][0]["omitted_posts"], 0)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)

    def test_large_interest_and_attempt_state_do_not_break_json_bound(self):
        self.call("channels", action="add", name="ai_newz")
        self.call("interests", text="и" * plugin.MAX_INTERESTS)
        posts = [{"id": f"ai_newz/{n}", "url": f"https://t.me/ai_newz/{n}", "text": "x" * 1200, "date": "", "links": []}
                 for n in range(1, 21)]
        with patch.object(plugin, "_fetch_channel", return_value=(posts, "")):
            result = self.call("fetch_posts", limit=20)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        self.assertGreater(result["channels"][0]["omitted_posts"], 0)

    def test_many_channels_can_record_every_returned_id(self):
        for number in range(12):
            self.call("channels", action="add", name=f"channel{number}")

        def page(channel):
            return ([{"id": f"{channel}/{n}", "url": f"https://t.me/{channel}/{n}",
                      "text": "x", "date": "", "links": []} for n in range(1, 21)], "")

        with patch.object(plugin, "_fetch_channel", side_effect=page):
            result = self.call("fetch_posts", limit=20)
        ids = [post["id"] for row in result["channels"] for post in row["posts"]]
        self.assertGreater(len(ids), 100)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=ids)
        self.assertTrue(receipt["ok"])
        self.assertEqual(len(receipt["post_ids"]), len(ids))

    def test_large_posts_defer_source_with_individual_fetch_recovery(self):
        for number in range(12):
            self.call("channels", action="add", name=f"channel{number}")

        def page(channel):
            return ([{"id": f"{channel}/1", "url": f"https://t.me/{channel}/1",
                      "text": "x" * 1200, "date": "", "links": ["https://example.org/" + "z" * 280] * 3}], "")

        with patch.object(plugin, "_fetch_channel", side_effect=page):
            result = self.call("fetch_posts", limit=20)
        self.assertTrue(result["output_limited"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        deferred = [row for row in result["channels"] if row["coverage"] == "output_deferred"]
        self.assertTrue(deferred)
        self.assertTrue(all(row["first_omitted_id"] == f"{row['channel']}/1" for row in deferred))
        with patch.object(plugin, "_fetch_channel", side_effect=page):
            recovered = self.call("fetch_posts", channel=deferred[0]["channel"])["channels"][0]
        self.assertEqual(recovered["coverage"], "recent_page_only")
        self.assertEqual(recovered["posts"][0]["id"], deferred[0]["first_omitted_id"])

    def test_escaped_interest_text_cannot_blow_output_cap(self):
        self.assertFalse(self.call("interests", text="\n" * 4000)["ok"])


if __name__ == "__main__":
    unittest.main()
