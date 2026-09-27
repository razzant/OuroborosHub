"""Consumer-facing, offline tests for the public preview and curated feed extension."""
from __future__ import annotations

import email.utils
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


class ConsumerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = FakeAPI(self.tmp.name)
        plugin.register(self.api)

    def call(self, tool_name, **kwargs):
        return json.loads(self.api.tools[tool_name][0](**kwargs))


class DigestConsumerTests(ConsumerCase):
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

    def test_overlapping_telegram_fetch_does_not_revoke_returned_ids(self):
        self.call("channels", action="add", name="ai_newz")

        def page(*ids):
            return ([{"id": f"ai_newz/{n}", "url": f"https://t.me/ai_newz/{n}",
                      "text": str(n), "date": "", "links": []} for n in ids], "")

        def interleave(_channel):
            with patch.object(plugin, "_fetch_channel", return_value=page(50, 51)):
                newer = self.call("fetch_posts", channel="ai_newz")["channels"][0]
            self.assertEqual([p["id"] for p in newer["posts"]], ["ai_newz/50", "ai_newz/51"])
            return page(1, 2)

        with patch.object(plugin, "_fetch_channel", side_effect=interleave):
            stale = self.call("fetch_posts", channel="ai_newz")["channels"][0]
        self.assertTrue(stale["stale_read"])
        self.assertEqual(stale["lost_unattempted_now"], 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=["ai_newz/50", "ai_newz/51"])["ok"])

    def test_newer_attempt_between_source_and_serialization_fences_stale_ids(self):
        self.call("channels", action="add", name="ai_newz")

        def page(n):
            return ([{"id": f"ai_newz/{n}", "url": f"https://t.me/ai_newz/{n}",
                      "text": str(n), "date": "", "links": []}], "")

        original = plugin._telegram_source
        def interleave(channel, limit, include_attempted, watermark):
            old = original(channel, limit, include_attempted, watermark)
            with patch.object(plugin, "_telegram_source", original), patch.object(
                    plugin, "_fetch_channel", return_value=page(2)):
                newer = self.call("fetch_posts", channel=channel)["channels"][0]
            self.assertEqual(newer["posts"][0]["id"], "ai_newz/2")
            self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                      post_ids=["ai_newz/2"])["ok"])
            return old

        with patch.object(plugin, "_fetch_channel", return_value=page(1)), patch.object(
                plugin, "_telegram_source", side_effect=interleave):
            stale = self.call("fetch_posts", channel="ai_newz")["channels"][0]
        self.assertTrue(stale["stale_read"])
        self.assertEqual(stale["posts"], [])
        self.assertEqual(stale["lost_unattempted_now"], 0)
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT id FROM observed WHERE channel='ai_newz'").fetchall()[0][0], 2)

    def test_legacy_observed_rows_migrate_without_losing_ids(self):
        path = Path(self.api.root) / "digest.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE observed (channel TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(channel,id))")
            db.execute("INSERT INTO observed VALUES ('ai_newz',12)")
        self.call("channels", action="add", name="ai_newz")
        with sqlite3.connect(path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(observed)")}
            self.assertIn("lost_reported", columns)
            self.assertEqual(db.execute("SELECT id FROM observed WHERE channel='ai_newz'").fetchone()[0], 12)

    def test_existing_receipt_survives_source_removal(self):
        self.call("channels", action="add", name="ai_newz")
        page = ([{"id": "ai_newz/12", "url": "https://t.me/ai_newz/12",
                  "text": "post", "date": "", "links": []}], "")
        with patch.object(plugin, "_fetch_channel", return_value=page):
            self.call("fetch_posts")
        first = self.call("record_attempt", day="2026-09-27", post_ids=["ai_newz/12"])
        self.assertTrue(first["new_attempt"])
        self.call("channels", action="remove", name="ai_newz")
        again = self.call("record_attempt", day="2026-09-27", post_ids=["ai_newz/12"])
        self.assertFalse(again["new_attempt"])
        self.assertTrue(again["same_selection"])
        self.assertEqual(again["status"], "already_attempted_do_not_auto_publish")

    def test_twenty_one_parsed_posts_are_not_falsely_lost(self):
        self.call("channels", action="add", name="ai_newz")
        page = b"".join((f'<div class="tgme_widget_message" data-post="ai_newz/{n}">'
                         f'<div class="tgme_widget_message_text">Post {n}</div></div>').encode()
                        for n in range(1, 22))
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=page)):
            first = self.call("fetch_posts", limit=20)["channels"][0]
            self.assertEqual(first["posts"][0]["id"], "ai_newz/1")
            self.assertEqual(first["omitted_posts"], 1)
            again = self.call("fetch_posts", limit=20)["channels"][0]
        self.assertEqual(again["lost_unattempted_now"], 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[p["id"] for p in first["posts"]])["ok"])

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


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
OPENAI = "https://openai.com/news/rss.xml"
DEEPMIND = "https://deepmind.google/blog/rss.xml"


def rss(*items):
    """items: (guid, hours_ago | None, title[, description])."""
    rows = []
    for guid, hours_ago, title, *rest in items:
        stamp = "" if hours_ago is None else "<pubDate>%s</pubDate>" % email.utils.format_datetime(
            NOW - timedelta(hours=hours_ago), usegmt=True)
        link = "<link>%s</link>" % guid if guid.startswith("https://") else ""
        guid_tag = "<guid>%s</guid>" % guid if guid else ""
        rows.append("<item><title><![CDATA[%s]]></title><description><![CDATA[%s]]></description>%s%s%s</item>"
                    % (title, rest[0] if rest else "summary of " + title, link, guid_tag, stamp))
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>OpenAI News</title>'
            '<link>https://openai.com/news</link>%s</channel></rss>' % "".join(rows)).encode()


def feed_id(guid, feed="openai_news"):
    return f"rss:{feed}/" + hashlib.sha256(guid.encode()).hexdigest()[:20]


def article(slug):
    return f"https://openai.com/index/{slug}"


def feed_response(content, **kwargs):
    kwargs.setdefault("url", OPENAI)
    kwargs.setdefault("content_type", "text/xml; charset=utf-8")
    return FakeResponse(content=content, **kwargs)


def route(pages):
    def opened(request, timeout):
        page = pages[request.full_url]
        if isinstance(page, Exception):
            raise page
        return page
    return opened


class FeedSourceTests(ConsumerCase):
    def setUp(self):
        super().setUp()
        clock = patch.object(plugin, "_utcnow", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def fetch(self, pages, **kwargs):
        with patch.object(plugin._OPENER, "open", side_effect=route(pages)) as opened:
            result = self.call("fetch_posts", **kwargs)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        return result, [call.args[0].full_url for call in opened.call_args_list]

    def source(self, result, name="rss:openai_news"):
        return next(row for row in result["channels"] if row["channel"] == name)

    def test_only_curated_exact_feeds_can_be_configured(self):
        added = self.call("channels", action="add", name="rss:OpenAI_News")
        self.assertEqual(added["channels"], ["rss:openai_news"])
        self.assertEqual(added["curated_feeds"], {"rss:openai_news": OPENAI, "rss:deepmind": DEEPMIND})
        self.assertEqual(self.call("channels", action="add", name=OPENAI)["channels"], ["rss:openai_news"])
        for name in ("rss:unknown", "http://openai.com/news/rss.xml", "https://openai.com/news/rss.xml?x=1",
                     "https://openai.com/blog/rss.xml", "https://evil.test/rss.xml", "rss:../../etc",
                     "rss:google_research", "https://research.google/blog/rss/"):
            with self.subTest(name=name):
                refused = self.call("channels", action="add", name=name)
                self.assertFalse(refused["ok"])
        self.assertEqual(self.call("channels", action="remove", name=OPENAI)["channels"], [])
        self.assertFalse(self.call("fetch_posts", channel="https://evil.test/rss.xml")["ok"])

    def test_deepmind_feed_shape_is_its_own_recordable_source(self):
        self.assertEqual(self.call("channels", action="add", name=DEEPMIND)["channels"], ["rss:deepmind"])
        post = "https://deepmind.google/blog/introducing-a-model/"
        # Real shape: namespaced media/atom elements, an empty description, text/xml without charset.
        feed = ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
                'xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Google DeepMind News</title>'
                '<atom:link href="https://example.com/blog/rss.xml" rel="self"/><item><title>Introducing a model</title>'
                '<link>%s</link><description/><pubDate>%s</pubDate><guid>%s</guid>'
                '<media:thumbnail url="https://lh3.googleusercontent.com/x"/></item></channel></rss>'
                % (post, email.utils.format_datetime(NOW - timedelta(hours=3)), post)).encode()
        response = feed_response(feed, url=DEEPMIND, content_type="text/xml")
        source = self.source(self.fetch({DEEPMIND: response})[0], "rss:deepmind")
        self.assertEqual(source["coverage"], "feed_window")
        [item] = source["posts"]
        self.assertEqual((item["id"], item["url"], item["title"], item["text"]),
                         (feed_id(post, "deepmind"), post, "Introducing a model", ""))
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=[item["id"]])["ok"])
        self.assertEqual(self.source(self.fetch({DEEPMIND: response})[0], "rss:deepmind")["posts"], [])

    def test_mixed_fetch_returns_bounded_cited_records_and_attempt_is_not_delivery(self):
        self.call("channels", action="add", name="ai_newz")
        self.call("channels", action="add", name="rss:openai_news")
        feed = rss((article("fresh"), 5, "Fresh model card", "<p>System card &amp; <b>evals</b></p>"),
                   (article("older"), 30, "Older post"),
                   (article("launch-2015"), 24 * 365 * 10, "Archive item"))
        pages = {"https://t.me/s/ai_newz": FakeResponse(), OPENAI: feed_response(feed)}
        result, urls = self.fetch(pages, limit=5)
        self.assertEqual(sorted(urls), sorted(pages))
        self.assertTrue(result["ok"])
        telegram, source = self.source(result, "ai_newz"), self.source(result)
        self.assertEqual((telegram["kind"], source["kind"]), ("telegram", "rss"))
        self.assertEqual(source["coverage"], "feed_window")
        # Oldest first; the publisher archive before the subscription window is not a backlog.
        self.assertEqual([p["id"] for p in source["posts"]], [feed_id(article("older")), feed_id(article("fresh"))])
        fresh = source["posts"][1]
        self.assertEqual(fresh["url"], article("fresh"))
        self.assertEqual(fresh["title"], "Fresh model card")
        self.assertEqual(fresh["text"], "System card & evals")
        self.assertEqual(fresh["date"], (NOW - timedelta(hours=5)).isoformat())
        self.assertFalse(source["possible_gap"])
        self.assertEqual(source["window_since"], (NOW - timedelta(days=7)).isoformat())
        ids = [p["id"] for row in result["channels"] for p in row["posts"]]
        self.assertFalse(self.call("record_attempt", day="2026-09-27", post_ids=[feed_id(article("launch-2015"))])["ok"])
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=ids)
        self.assertEqual((receipt["status"], len(receipt["post_ids"])), ("attempted_not_delivered", 4))
        again, _ = self.fetch(pages)
        self.assertEqual([row["posts"] for row in again["channels"]], [[], []])
        self.assertEqual(self.source(again)["attempted_through"], fresh["date"])
        # A newer item after a replayed receipt stays unseen: replay claims nothing.
        newer = rss((article("newest"), 1, "Newest"), (article("fresh"), 5, "Fresh model card"))
        pages[OPENAI] = feed_response(newer)
        pending = self.source(self.fetch(pages)[0])["posts"]
        replay = self.call("record_attempt", day="2026-09-27", post_ids=[p["id"] for p in pending])
        self.assertEqual(replay["status"], "already_attempted_do_not_auto_publish")
        self.assertEqual([p["id"] for p in self.source(self.fetch(pages)[0])["posts"]], [feed_id(article("newest"))])

    def test_source_failures_stay_source_specific(self):
        self.call("channels", action="add", name="ai_newz")
        self.call("channels", action="add", name="rss:openai_news")
        feed = feed_response(rss((article("a"), 2, "A")))
        refused = plugin.urllib.error.HTTPError(OPENAI, 403, "Forbidden", {}, None)
        for pages, failed, readable in (
                ({"https://t.me/s/ai_newz": FakeResponse(), OPENAI: refused}, "rss:openai_news", "ai_newz"),
                ({"https://t.me/s/ai_newz": TimeoutError(), OPENAI: feed}, "ai_newz", "rss:openai_news")):
            with self.subTest(failed=failed):
                result, _ = self.fetch(pages)
                self.assertTrue(result["ok"])
                self.assertEqual(self.source(result, failed)["coverage"], "unavailable")
                self.assertIn("unavailable", self.source(result, failed)["error"])
                self.assertTrue(self.source(result, readable)["posts"])
        both_down = {"https://t.me/s/ai_newz": TimeoutError(), OPENAI: refused}
        self.assertFalse(self.fetch(both_down)[0]["ok"])

    def test_reordered_and_duplicated_feed_never_loses_unseen_items(self):
        self.call("channels", action="add", name="rss:openai_news")
        first = rss((article("c"), 3, "C"), (article("b"), 20, "B"), (article("a"), 40, "A"),
                    (article("a"), 40, "A repeated with the same date"))
        source = self.source(self.fetch({OPENAI: feed_response(first)}, limit=2)[0])
        self.assertEqual([p["id"] for p in source["posts"]], [feed_id(article("a")), feed_id(article("b"))])
        self.assertEqual((source["omitted_posts"], source["first_omitted_id"]), (1, feed_id(article("c"))))
        self.assertEqual(source["skipped_items"]["duplicate"], 1)
        # Feed items are independent: attempting B alone does not skip A.
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=[feed_id(article("b"))])["ok"])
        shuffled = rss((article("b"), 20, "B retitled"), (article("d"), 60, "Backdated D"), (article("c"), 3, "C"),
                       (article("a"), 40, "A"), (article("b"), 20, "B"))
        source = self.source(self.fetch({OPENAI: feed_response(shuffled)}, limit=20)[0])
        self.assertEqual([p["id"] for p in source["posts"]],
                         [feed_id(article("d")), feed_id(article("a")), feed_id(article("c"))])
        self.assertEqual(source["lost_unattempted_now"], 0)
        self.assertFalse(source["possible_gap"])

    def test_vanished_returned_item_is_disclosed_and_remains_acknowledgeable(self):
        self.call("channels", action="add", name="rss:openai_news")
        self.fetch({OPENAI: feed_response(rss((article("a"), 5, "A"), (article("b"), 4, "B")))})
        source = self.source(self.fetch({OPENAI: feed_response(rss((article("b"), 4, "B")))})[0])
        self.assertEqual((source["lost_unattempted_now"], source["historical_lost_unattempted"]), (1, 1))
        self.assertTrue(source["possible_gap"])
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("a")), feed_id(article("b"))])["ok"])

    def test_omitted_item_that_vanishes_reports_loss_without_becoming_recordable(self):
        self.call("channels", action="add", name="rss:openai_news")
        first = self.source(self.fetch({OPENAI: feed_response(rss(
            (article("new"), 1, "New"), (article("old"), 2, "Old")))}, limit=1)[0])
        self.assertEqual([p["id"] for p in first["posts"]], [feed_id(article("old"))])
        self.assertEqual(first["first_omitted_id"], feed_id(article("new")))
        self.assertFalse(self.call("record_attempt", day="2026-09-27",
                                   post_ids=[feed_id(article("new"))])["ok"])
        later = self.source(self.fetch({OPENAI: feed_response(rss((article("old"), 2, "Old")))})[0])
        self.assertEqual(later["lost_unattempted_now"], 1)
        self.assertTrue(later["possible_gap"])

    def test_redated_returned_item_stays_eligible_when_date_moves_behind_window(self):
        self.call("channels", action="add", name="rss:openai_news")
        first = self.source(self.fetch({OPENAI: feed_response(rss((article("a"), 2, "A")))})[0])
        self.assertEqual([p["id"] for p in first["posts"]], [feed_id(article("a"))])
        corrected = self.source(self.fetch({OPENAI: feed_response(rss((article("a"), 20 * 24, "A")))})[0])
        self.assertLess((NOW - timedelta(hours=20 * 24)).isoformat(), corrected["window_since"])
        self.assertEqual([p["id"] for p in corrected["posts"]], [feed_id(article("a"))])
        self.assertEqual(corrected["lost_unattempted_now"], 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("a"))])["ok"])

    def test_temporarily_undated_pending_item_is_not_falsely_lost(self):
        self.call("channels", action="add", name="rss:openai_news")
        self.fetch({OPENAI: feed_response(rss((article("a"), 2, "A"), (article("b"), 1, "B")))})
        undated = self.source(self.fetch({OPENAI: feed_response(rss(
            (article("a"), None, "A"), (article("b"), 1, "B")))})[0])
        self.assertEqual(undated["lost_unattempted_now"], 0)
        self.assertTrue(undated["possible_gap"])
        restored = self.source(self.fetch({OPENAI: feed_response(rss(
            (article("a"), 2, "A"), (article("b"), 1, "B")))})[0])
        self.assertEqual(restored["lost_unattempted_now"], 0)
        self.assertIn(feed_id(article("a")), [p["id"] for p in restored["posts"]])

    def test_fresh_duplicate_guid_outweighs_stale_first_occurrence(self):
        self.call("channels", action="add", name="rss:openai_news")
        duplicated = rss((article("a"), 20, "Stale"), (article("a"), 1, "Fresh"))
        source = self.source(self.fetch({OPENAI: feed_response(duplicated)})[0])
        self.assertEqual([p["id"] for p in source["posts"]], [feed_id(article("a"))])
        self.assertEqual(source["posts"][0]["title"], "Fresh")
        self.assertEqual(source["skipped_items"]["duplicate"], 1)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("a"))])["ok"])

    def test_stale_overlapping_fetch_cannot_erase_newer_returned_id(self):
        self.call("channels", action="add", name="rss:openai_news")
        old_feed = plugin._Feed()
        old_feed.parser.Parse(rss((article("old"), 2, "Old")), True)
        old = (old_feed.items, {"feed_truncated": False}, "")
        fresh = plugin._Feed()
        fresh.parser.Parse(rss((article("new"), 1, "New")), True)
        calls = 0
        def overlapping(_name):
            nonlocal calls
            calls += 1
            if calls == 1:
                # First read starts, a second read completes while it waits,
                # then the first returns its stale empty feed snapshot.
                newer = self.call("fetch_posts")
                self.assertEqual([p["id"] for p in self.source(newer)["posts"]],
                                 [feed_id(article("new"))])
                return old
            return fresh.items, {"feed_truncated": False}, ""
        with patch.object(plugin, "_fetch_feed", side_effect=overlapping):
            stale = self.source(self.call("fetch_posts"))
        self.assertTrue(stale["stale_read"])
        self.assertEqual(stale["lost_unattempted_now"], 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("new"))])["ok"])

    def test_newer_fetch_between_selection_and_return_does_not_erase_issued_id(self):
        self.call("channels", action="add", name="rss:openai_news")
        old_feed = plugin._Feed()
        old_feed.parser.Parse(rss((article("old"), 2, "Old")), True)
        new_feed = plugin._Feed()
        new_feed.parser.Parse(rss((article("new"), 1, "New")), True)
        original_source = plugin._feed_source
        first = True
        def overlapping(source, limit, attempted):
            nonlocal first
            row = original_source(source, limit, attempted)
            if first:
                first = False
                with patch.object(plugin, "_fetch_feed", return_value=(new_feed.items, {}, "")):
                    newer = self.call("fetch_posts")
                self.assertEqual([p["id"] for p in self.source(newer)["posts"]],
                                 [feed_id(article("new"))])
            return row
        with patch.object(plugin, "_fetch_feed", return_value=(old_feed.items, {}, "")), \
                patch.object(plugin, "_feed_source", side_effect=overlapping):
            earlier = self.call("fetch_posts")
        self.assertEqual([p["id"] for p in self.source(earlier)["posts"]],
                         [feed_id(article("old"))])
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("old")), feed_id(article("new"))])["ok"])

    def test_attempt_claims_write_transaction_before_returned_id_validation(self):
        self.call("channels", action="add", name="rss:openai_news")
        self.fetch({OPENAI: feed_response(rss((article("a"), 1, "A")))})
        statements = []
        original_connect = plugin.sqlite3.connect
        def traced_connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(plugin.sqlite3, "connect", side_effect=traced_connect):
            receipt = self.call("record_attempt", day="2026-09-27",
                                post_ids=[feed_id(article("a"))])
        self.assertTrue(receipt["new_attempt"])
        claim = next(i for i, sql in enumerate(statements) if sql == "BEGIN IMMEDIATE")
        validation = next(i for i, sql in enumerate(statements) if "SELECT 1 FROM feed_items" in sql)
        self.assertLess(claim, validation)

    def test_malformed_hostile_or_changed_feeds_are_explicit_failures(self):
        self.call("channels", action="add", name="rss:openai_news")
        bomb = ('<!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                '<rss version="2.0"><channel><item><title>&b;</title><guid>x</guid></item></channel></rss>').encode()
        cases = {
            "not well formed": feed_response(b"<rss><channel><item><title>x</item></channel></rss>"),
            "entity expansion": feed_response(bomb),
            "atom instead of rss": feed_response(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>'),
            "html challenge": feed_response(b"<html>Just a moment...</html>", content_type="text/html"),
            "unexpected url": feed_response(rss((article("a"), 1, "A")), url="https://openai.com/other.xml"),
            "empty channel": feed_response(rss()),
            "no identities": feed_response(rss(("", 1, "No guid or link"))),
            "no dates": feed_response(rss((article("a"), None, "Undated"))),
        }
        for label, response in cases.items():
            with self.subTest(label):
                result, _ = self.fetch({OPENAI: response})
                self.assertFalse(result["ok"])
                self.assertEqual(self.source(result)["coverage"], "unavailable")
                self.assertTrue(self.source(result)["error"])
                self.assertEqual(self.source(result)["posts"], [])
        partial = self.source(self.fetch({OPENAI: feed_response(rss((article("a"), 1, "A"), (article("u"), None, "U")))})[0])
        self.assertEqual(partial["skipped_items"]["undated"], 1)
        self.assertEqual([p["id"] for p in partial["posts"]], [feed_id(article("a"))])

    def test_unknown_feed_encoding_is_source_local_failure(self):
        self.call("channels", action="add", name="rss:openai_news")
        self.call("channels", action="add", name="rss:deepmind")
        invalid = b'<?xml version="1.0" encoding="X-UNKNOWN"?><rss version="2.0"><channel/></rss>'
        valid = rss(("https://deepmind.google/blog/valid/", 2, "Valid"))
        result, _ = self.fetch({OPENAI: feed_response(invalid),
                                DEEPMIND: feed_response(valid, url=DEEPMIND)})
        self.assertEqual(self.source(result)["coverage"], "unavailable")
        self.assertEqual(self.source(result, "rss:deepmind")["coverage"], "feed_window")

    def test_oversized_feed_keeps_complete_prefix_and_discloses_gap(self):
        self.call("channels", action="add", name="rss:openai_news")
        items = [(article(f"n{n}"), n, f"Item {n}", "y" * 200) for n in range(1, 60)]
        big = rss(*items)
        with patch.object(plugin, "MAX_FEED_BYTES", 4096):
            source = self.source(self.fetch({OPENAI: feed_response(big)}, limit=20)[0])
            self.assertTrue(source["feed_truncated"])
            self.assertEqual(source["coverage"], "feed_window")
            returned = [p["id"] for p in source["posts"]]
            self.assertTrue(returned)
            self.assertEqual(returned[-1], feed_id(article("n1")))
            # The readable prefix stops well inside the 7-day window: disclose it.
            self.assertTrue(source["possible_gap"])
            tiny = self.fetch({OPENAI: feed_response(b'<?xml version="1.0"?><rss version="2.0"><channel>' + b" " * 5000)})[0]
            self.assertEqual(self.source(tiny)["coverage"], "unavailable")
        with patch.object(plugin, "MAX_FEED_ITEMS", 3):
            capped = self.source(self.fetch({OPENAI: feed_response(big)}, limit=20)[0])
        self.assertTrue(capped["feed_truncated"])
        self.assertEqual(len(capped["posts"]), 3)

    def test_truncated_prefix_does_not_claim_an_observed_item_was_lost(self):
        self.call("channels", action="add", name="rss:openai_news")
        initial = rss((article("recent"), 1, "Recent"), (article("older"), 2, "Older"))
        self.fetch({OPENAI: feed_response(initial)})
        # The older item may remain beyond a byte-limited prefix. It must not
        # be deleted or counted as definitely lost merely for being unseen.
        with patch.object(plugin, "MAX_FEED_BYTES", len(rss((article("recent"), 1, "Recent"))) + 10):
            source = self.source(self.fetch({OPENAI: feed_response(initial)})[0])
        self.assertTrue(source["feed_truncated"])
        self.assertTrue(source["possible_gap"])
        self.assertEqual(source["lost_unattempted_now"], 0)
        restored = self.source(self.fetch({OPENAI: feed_response(initial)})[0])
        self.assertEqual({p["id"] for p in restored["posts"]},
                         {feed_id(article("recent")), feed_id(article("older"))})

    def test_final_serialized_output_is_bounded_at_exact_limit(self):
        self.call("channels", action="add", name="rss:openai_news")
        page = {OPENAI: feed_response(rss((article("a"), 1, "A", "x" * 400)))}
        normal, _ = self.fetch(page)
        limit = len(json.dumps(normal, ensure_ascii=False)) - 1
        with patch.object(plugin, "MAX_OUTPUT_CHARS", limit):
            result, _ = self.fetch(page)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), limit)

    def test_twelve_source_pressure_defers_feed_with_individual_recovery(self):
        for number in range(11):
            self.call("channels", action="add", name=f"channel{number}")
        self.call("channels", action="add", name="rss:openai_news")
        self.assertIn("limit", self.call("channels", action="add", name="rss:deepmind")["error"])

        def page(channel):
            return ([{"id": f"{channel}/{n}", "url": f"https://t.me/{channel}/{n}", "text": "x" * 600,
                      "date": "", "links": []} for n in range(1, 21)], "")

        feed = rss(*[(article(f"p{n}"), n, f"Post {n}", "z" * 3000) for n in range(1, 21)])
        with patch.object(plugin, "_fetch_channel", side_effect=page), \
                patch.object(plugin._OPENER, "open", side_effect=route({OPENAI: feed_response(feed)})):
            result = self.call("fetch_posts", limit=20)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
            self.assertTrue(result["output_limited"])
            source = self.source(result)
            self.assertEqual(source["coverage"], "output_deferred")
            self.assertEqual(source["first_omitted_id"], feed_id(article("p20")))
            ids = [p["id"] for row in result["channels"] for p in row["posts"]]
            self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=ids)["ok"])
            recovered = self.source(self.call("fetch_posts", channel="rss:openai_news", limit=3))
        self.assertEqual(recovered["coverage"], "feed_window")
        self.assertEqual(recovered["posts"][0]["id"], feed_id(article("p20")))
        self.assertEqual(recovered["omitted_posts"], 17)

    def test_revision_readd_and_bounded_state_do_not_replay_attempts(self):
        self.call("channels", action="add", name="rss:openai_news")
        feed = feed_response(rss((article("a"), 50, "A"), (article("b"), 30, "B"), (article("c"), 10, "C")))
        ids = [p["id"] for p in self.source(self.fetch({OPENAI: feed})[0])["posts"]]
        with patch.object(plugin, "MAX_FEED_STATE", 2):
            self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=ids)["ok"])
        revision = self.source(self.fetch({OPENAI: feed}, include_attempted=True)[0])
        # A was pruned with the window moving past it; B and C are still attempted.
        self.assertEqual([p["id"] for p in revision["posts"]], ids[1:])
        self.assertTrue(all(p["already_attempted"] for p in revision["posts"]))
        self.call("channels", action="remove", name="rss:openai_news")
        self.call("channels", action="add", name="rss:openai_news")
        self.assertEqual(self.source(self.fetch({OPENAI: feed})[0])["posts"], [])


if __name__ == "__main__":
    unittest.main()
