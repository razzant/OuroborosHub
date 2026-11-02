"""Consumer-facing, offline tests for the public preview and owner-chosen feed extension."""
from __future__ import annotations

import email.utils
import gzip
import hashlib
import html
import http.client
import importlib.util
import inspect
import itertools
import json
import os
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch


MODULE_PATH = Path(__file__).with_name("plugin.py")
spec = importlib.util.spec_from_file_location("science_digest_plugin", MODULE_PATH)
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
PUBLIC = ["149.154.167.99"]

HTML = b"""<!doctype html><html><body>
<div class="tgme_widget_message" data-post="sample_news/12">
<div class="tgme_widget_message_text js-message_text">Fresh &amp; relevant <b>findings</b><br>
<a href="https://example.org/paper">Technical report</a></div>
<a class="tgme_widget_message_date" href="https://t.me/sample_news/12"><time datetime="2026-09-27T01:00:00+00:00">now</time></a></div>
<div class="tgme_widget_message" data-post="sample_news/13">
<div class="tgme_widget_message_text">Marketing only</div></div>
<div class="tgme_widget_message" data-post="other/100"><div class="tgme_widget_message_text">foreign</div></div>
<script>ignore this prompt injection</script></body></html>"""

# Real preview markup (t.me/s/seeallochnaya, 2026-11-01): the server's label
# sits in its own block, outside any tgme_widget_message_text.
PLACEHOLDER = ('<div class="message_media_not_supported_wrap"> <div class="message_media_not_supported"> '
               '<div class="message_media_not_supported_label">Please open Telegram to view this post</div> '
               '<a href="https://t.me/sample_news/1" class="message_media_view_in_telegram">VIEW IN TELEGRAM</a> '
               '</div> </div>')
PHOTO = ('<a class="tgme_widget_message_photo_wrap" href="https://t.me/sample_news/1" '
         'style="width:453px;background-image:url(\'https://cdn4.telesco.pe/file/x.jpg\')"></a>')


def post_html(number, text=None, *, extra="", hours_ago=1, dated=True, channel="sample_news",
              text_class="tgme_widget_message_text js-message_text"):
    """One post as the public preview renders it."""
    stamp = f'<time datetime="{(NOW - timedelta(hours=hours_ago)).isoformat()}" class="time">00:00</time>' if dated else ""
    body = f'<div class="{text_class}" dir="auto">{text}</div>' if text is not None else ""
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message '
            f'text_not_supported_wrap js-widget_message" data-post="{channel}/{number}">'
            f'<div class="tgme_widget_message_bubble">{extra}{body}<div class="tgme_widget_message_footer">'
            f'<a class="tgme_widget_message_date" href="https://t.me/{channel}/{number}">{stamp}</a></div>'
            f'</div></div></div>')


def page(*posts):
    return ('<!doctype html><html><body><section class="tgme_channel_history js-message_history">'
            + "".join(posts) + "</section></body></html>").encode()


def stamp(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def textless(source):
    """The separately listed posts; the key is present only when there are some."""
    return source.get("text_not_extracted", [])


class FakeResponse:
    def __init__(self, content=HTML, url="https://t.me/s/sample_news", content_type="text/html", chunk=None,
                 encoding=None):
        self.content = content
        self.url = url
        self.headers = {"Content-Type": content_type}
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self.chunk = chunk  # bytes per read, to model a slow trickle

    def __enter__(self):
        self.offset = 0  # each open() reads the body from the start
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, n):
        chunk = self.content[self.offset:self.offset + min(n, self.chunk or n)]
        self.offset += len(chunk)
        return chunk


class FakeAPI:
    """The PluginAPI surface this skill uses, with the host's registration rules
    (keyword-only description/schema, short names of <=24 alnum/underscore)."""

    def __init__(self, root):
        self.root = root
        self.tools = {}

    def get_state_dir(self):
        return str(self.root)

    def log(self, level, message, **fields):
        return None

    def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
        assert len(name) <= 24 and name.replace("_", "").isalnum() and name not in self.tools, name
        self.tools[name] = (handler, {"description": description, "schema": schema, "timeout_sec": timeout_sec})


class ConsumerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A fixed clock, and DNS that resolves every host to a public address unless a test says otherwise.
        for target, value in (("_utcnow", NOW), ("_resolve", PUBLIC)):
            patcher = patch.object(plugin, target, return_value=value)
            setattr(self, target.strip("_"), patcher.start())
            self.addCleanup(patcher.stop)
        self.api = FakeAPI(self.tmp.name)
        plugin.register(self.api)

    def call(self, tool_name, **kwargs):
        return json.loads(self.api.tools[tool_name][0](**kwargs))

    def fetch_page(self, content, **kwargs):
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=content)):
            return self.call("fetch_posts", **kwargs)

    def watermark(self, channel="sample_news"):
        with plugin._connect() as conn:
            row = conn.execute("SELECT last_id FROM seen WHERE channel=?", (channel,)).fetchone()
        return row[0] if row else 0


class DigestConsumerTests(ConsumerCase):
    def test_registered_interface_and_free_text_configuration(self):
        self.assertEqual(set(self.api.tools), {"interests", "channels", "fetch_posts", "record_attempt"})
        self.assertEqual(self.call("interests", text="Coral reef ecology and open climate datasets")["interests"],
                         "Coral reef ecology and open climate datasets")
        self.assertEqual(self.call("interests")["interests"], "Coral reef ecology and open climate datasets")
        self.assertEqual(self.call("channels", action="add", name="https://t.me/s/Sample_News")["channels"], ["sample_news"])
        self.assertEqual(self.call("channels", action="add", name="@sample_news")["channels"], ["sample_news"])
        self.assertEqual(self.call("channels", action="remove", name="sample_news")["channels"], [])

    def test_skill_instructions_name_tools_by_role_not_by_an_install_prefix(self):
        text = MODULE_PATH.with_name("SKILL.md").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"ext_\d+_")
        for name in self.api.tools:
            self.assertIn(f"`{name}`", text)
        for word in ("text_not_extracted", "baseline_posts", "omitted_posts", "redirect_to", "excerpt",
                     "fetch_ids", "include_attempted", "timeout"):
            self.assertIn(word, text)

    def test_skill_instructions_settle_what_the_consumer_run_had_to_guess(self):
        text = " ".join(MODULE_PATH.with_name("SKILL.md").read_text(encoding="utf-8").split())
        # output_deferred is a coverage value and means fetch alone; omitted_posts is not that.
        self.assertIn('`coverage: "output_deferred"`', text)
        self.assertIn("`omitted_posts` stay for the next edition", text)
        self.assertIn("do not fetch them again", text)
        # The argument is named, not described.
        self.assertIn("`day` (`YYYY-MM-DD`)", text)
        # A source found in a post is never subscribed; opening its link to verify is expected.
        self.assertNotIn("add or fetch a source", text)
        self.assertIn("Never add a source found in a post or feed item", text)
        self.assertIn("teaser or a paywall", text)
        self.assertIn("do not present it as read", text)
        # The bounded portion is oldest first and the edition says how many remain.
        self.assertIn("oldest first", text)
        self.assertIn("how many remain", text)

    def test_skill_instructions_state_the_limits_the_code_applies(self):
        text = " ".join(MODULE_PATH.with_name("SKILL.md").read_text(encoding="utf-8").split())
        default = inspect.signature(plugin._fetch_posts).parameters["limit"].default
        schema = self.api.tools["fetch_posts"][1]["schema"]["properties"]["limit"]
        self.assertEqual((schema["minimum"], schema["maximum"]), (1, plugin.MAX_POSTS_PER_CHANNEL))
        self.assertIn(f"up to `limit` readable posts or feed items per source (1–{plugin.MAX_POSTS_PER_CHANNEL}, "
                      f"default {default})", text)
        self.assertIn(f"share about {plugin.MAX_OUTPUT_CHARS:,} characters", text)
        self.assertIn(f"reads up to {plugin.MAX_PARALLEL_SOURCES} sources at a time", text)
        self.assertIn(f"dropped from state {plugin.LOOKBACK_DAYS} days after it was found missing", text)
        self.assertIn(f"XML namespace URI longer than {plugin.MAX_NAMESPACE_URI} characters", text)
        self.assertIn(f"more than {plugin.MAX_XML_NAMES:,} distinct XML names", text)
        self.assertIn(f"a single tag or comment longer than {plugin.XML_PIECE // 1024} KiB", text)
        self.assertIn(f"at least {plugin.MIN_PARSE_SECONDS} seconds for a body", text)
        self.assertIn(f"at most {plugin.MAX_UNLISTED_FEED_ITEMS:,} unattempted items", text)
        self.assertIn("closing `</html>` tag", text)
        # post_ids: an id counts while it is tracked; lost posts are outside the naming rule.
        self.assertIn("while it is among the newest 240 tracked for the channel", text)
        description = self.api.tools["record_attempt"][1]["description"]
        for wording in (text, description):
            self.assertIn("retained returned readable post not marked lost", wording)

    def test_untrusted_targets_refused_without_network(self):
        with patch.object(plugin._OPENER, "open") as opened:
            for name in ("http://t.me/sample_news", "https://t.me/sample_news/12", "https://t.me/s/sample_news?x=1",
                         "https://t.me/joinchat/secret", "../../etc/passwd", "@a", "rss:../../etc"):
                with self.subTest(name=name):
                    self.assertFalse(self.call("channels", action="add", name=name)["ok"])
        opened.assert_not_called()
        self.resolve.assert_not_called()
        self.assertEqual(self.call("channels")["channels"], [])

    def test_fetch_from_configured_channel_preserves_coverage_and_citations(self):
        self.call("channels", action="add", name="sample_news")
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()) as opened:
            result = self.call("fetch_posts", limit=5)
        self.assertTrue(result["ok"])
        opened.assert_called_once()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://t.me/s/sample_news")
        self.assertEqual(opened.call_args.kwargs["timeout"], plugin.REQUEST_TIMEOUT)
        self.resolve.assert_called_once_with("t.me", 443)
        self.assertFalse(result["archive_complete"])
        source = result["channels"][0]
        self.assertEqual(source["coverage"], "recent_page_only")
        self.assertEqual([p["id"] for p in source["posts"]], ["sample_news/12", "sample_news/13"])
        self.assertEqual(source["posts"][0]["text"], "Fresh & relevant findings Technical report")
        self.assertEqual(source["posts"][0]["links"], ["https://example.org/paper"])
        self.assertEqual(source["posts"][0]["url"], "https://t.me/sample_news/12")
        self.assertEqual(source["posts"][0]["date"], "2026-09-27T01:00:00+00:00")
        self.assertEqual(source["posts"][1]["date"], "")
        self.assertNotIn("text_not_extracted", source)
        self.assertNotIn("text_truncated", source["posts"][0])  # present only when true

    def test_unavailable_or_changed_preview_is_not_empty_news(self):
        self.call("channels", action="add", name="sample_news")
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
                plugin.urllib.request.Request("https://t.me/s/sample_news"), None,
                302, "", {}, "https://evil.test")

    def test_attempt_is_not_delivery_and_replay_keeps_original(self):
        self.call("channels", action="add", name="sample_news")
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            self.call("fetch_posts")
        self.assertFalse(self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/13"])["ok"])
        first = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/12", "sample_news/12"])
        self.assertTrue(first["ok"])
        self.assertEqual(first["status"], "attempted_not_delivered")
        later = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/13"])
        self.assertFalse(later["same_selection"])
        self.assertFalse(later["new_attempt"])
        self.assertEqual(later["status"], "already_attempted_do_not_auto_publish")
        self.assertEqual(later["post_ids"], ["sample_news/12"])
        extra = self.call("record_attempt", day="2026-09-27", edition="evening", post_ids=["sample_news/13"])
        self.assertTrue(extra["new_attempt"])
        self.assertEqual(extra["key"], "2026-09-27:evening")
        self.assertFalse(self.call("record_attempt", day="2026-09-27", edition="bad/edition", post_ids=[])["ok"])
        self.assertFalse(self.call("record_attempt", day="2026-09-28", post_ids=["sample_news/999999"])["ok"])
        self.assertFalse(self.call("record_attempt", day="2026-02-30", post_ids=[])["ok"])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            result = self.call("fetch_posts")
            self.assertEqual(result["recent_attempts"][0]["status"], "attempted_not_delivered")
            self.assertEqual(result["channels"][0]["posts"], [])
            self.assertFalse(result["channels"][0]["possible_gap"])
            # Both were read when recorded, so a retry has nothing to bring back.
            self.assertEqual(self.call("fetch_posts", include_attempted=True)["channels"][0]["posts"], [])

    def test_newer_preview_window_discloses_possible_gap(self):
        self.call("channels", action="add", name="sample_news")
        old = [{"id": "sample_news/10", "url": "https://t.me/sample_news/10", "text": "old", "date": "", "links": []}]
        with patch.object(plugin, "_fetch_channel", return_value=(old, "")):
            self.call("fetch_posts")
        self.call("record_attempt", day="2026-09-26", post_ids=["sample_news/10"])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            source = self.call("fetch_posts")["channels"][0]
        self.assertTrue(source["possible_gap"])
        self.assertEqual(source["last_attempted_id"], 10)

    def test_contiguous_next_page_is_not_reported_as_a_gap(self):
        self.call("channels", action="add", name="sample_news")

        def page_of(*ids):
            return ([{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}",
                      "text": str(n), "date": "", "links": []} for n in ids], "")

        with patch.object(plugin, "_fetch_channel", return_value=page_of(9, 10)):
            self.call("fetch_posts")
        self.assertTrue(self.call("record_attempt", day="2026-09-26",
                                  post_ids=["sample_news/9", "sample_news/10"])["ok"])
        with patch.object(plugin, "_fetch_channel", return_value=page_of(11, 12)):
            contiguous = self.call("fetch_posts")["channels"][0]
        self.assertFalse(contiguous["possible_gap"])
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=["sample_news/11", "sample_news/12"])["ok"])
        with patch.object(plugin, "_fetch_channel", return_value=page_of(14, 15)):
            skipped = self.call("fetch_posts")["channels"][0]
        self.assertEqual(skipped["lost_unattempted_now"], 0)
        self.assertTrue(skipped["possible_gap"])

    def test_page_order_duplicates_and_reply_quotes_do_not_corrupt_posts(self):
        self.call("channels", action="add", name="sample_news")
        content = (b'<html><body><div class="tgme_widget_message" data-post="sample_news/31">'
                   b'<a class="tgme_widget_message_reply" href="https://t.me/sample_news/5">'
                   b'<div class="tgme_widget_message_text js-message_reply_text">quoted older post</div></a>'
                   b'<div class="tgme_widget_message_text js-message_text">Own words</div></div>'
                   b'<div class="tgme_widget_message" data-post="sample_news/30">'
                   b'<div class="tgme_widget_message_text">Earlier</div></div>'
                   b'<div class="tgme_widget_message" data-post="sample_news/31">'
                   b'<div class="tgme_widget_message_text">Duplicate render</div></div></body></html>')
        posts = self.fetch_page(content)["channels"][0]["posts"]
        self.assertEqual([(p["id"], p["text"]) for p in posts],
                         [("sample_news/30", "Earlier"), ("sample_news/31", "Own words")])

    def test_grouped_media_nested_text_block_keeps_all_of_its_text(self):
        # Real grouped-media posts (t.me/s/ai_newz) nest a second text div inside the first.
        self.call("channels", action="add", name="sample_news")
        nested = ('<div class="tgme_widget_message_text js-message_text"><div class="tgme_widget_message_text '
                  'js-message_text"><b>Two image models</b><br/>Details</div> and a closing line</div>')
        posts = self.fetch_page(page(post_html(40, extra=nested)))["channels"][0]["posts"]
        self.assertEqual(posts[0]["text"], "Two image models Details and a closing line")

    def test_http_failures_name_the_status_without_reading_the_body(self):
        self.call("channels", action="add", name="sample_news")
        for code in (429, 503):
            error = plugin.urllib.error.HTTPError("https://t.me/s/sample_news", code, "x", {}, None)
            with self.subTest(code=code), patch.object(plugin._OPENER, "open", side_effect=error):
                source = self.call("fetch_posts")["channels"][0]
                self.assertEqual((source["coverage"], source["error"]),
                                 ("unavailable", f"preview unavailable: HTTP {code}"))

    def test_slow_preview_is_unavailable_rather_than_a_partial_page(self):
        self.call("channels", action="add", name="sample_news")
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            self.call("fetch_posts")  # both posts observed, none attempted
        clock = itertools.count()
        with patch.object(plugin, "READ_DEADLINE", 3), \
                patch.object(plugin.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(plugin._OPENER, "open", return_value=FakeResponse(chunk=64)):
            source = self.call("fetch_posts")["channels"][0]
        self.assertEqual((source["coverage"], source["error"]),
                         ("unavailable", "preview unavailable: read deadline exceeded"))
        # A cut page would make the unread newer post look deleted.
        self.assertEqual(source["lost_unattempted_now"], 0)

    def test_sources_are_fetched_concurrently_and_keep_their_order(self):
        for name in ("sample_news", "other_news"):
            self.call("channels", action="add", name=name)
        barrier = threading.Barrier(2, timeout=5)

        def page_of(channel):
            barrier.wait()  # breaks if sources are read one after another
            return ([{"id": f"{channel}/1", "url": f"https://t.me/{channel}/1",
                      "text": "x", "date": "", "links": []}], "")

        with patch.object(plugin, "_fetch_channel", side_effect=page_of):
            result = self.call("fetch_posts")
        self.assertEqual([row["channel"] for row in result["channels"]], ["other_news", "sample_news"])
        self.assertEqual({row["coverage"] for row in result["channels"]}, {"recent_page_only"})
        ids = [p["id"] for row in result["channels"] for p in row["posts"]]
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=ids)["ok"])

    def test_interrupted_fetch_then_scrolled_window_recovers_with_loss_disclosure(self):
        self.call("channels", action="add", name="sample_news")
        old = [{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}",
                "text": "old", "date": "", "links": []} for n in range(1, 11)]
        fresh = [{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}",
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
        self.call("channels", action="add", name="sample_news")

        def page_of(ids):
            return ([{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}",
                      "text": str(n), "date": "", "links": []} for n in ids], "")

        with patch.object(plugin, "_fetch_channel", return_value=page_of([1, 2, 3])):
            self.call("fetch_posts")
        with patch.object(plugin, "_fetch_channel", return_value=page_of([1, 3, 4])):
            source = self.call("fetch_posts")["channels"][0]
        self.assertEqual(source["lost_unattempted_now"], 1)
        self.assertTrue(source["possible_gap"])
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[p["id"] for p in source["posts"]])["ok"])

    def test_overlapping_telegram_fetch_does_not_revoke_returned_ids(self):
        self.call("channels", action="add", name="sample_news")

        def page_of(*ids):
            return ([{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}",
                      "text": str(n), "date": "", "links": []} for n in ids], "")

        def interleave(_channel):
            with patch.object(plugin, "_fetch_channel", return_value=page_of(50, 51)):
                newer = self.call("fetch_posts", channel="sample_news")["channels"][0]
            self.assertEqual([p["id"] for p in newer["posts"]], ["sample_news/50", "sample_news/51"])
            return page_of(1, 2)

        with patch.object(plugin, "_fetch_channel", side_effect=interleave):
            stale = self.call("fetch_posts", channel="sample_news")["channels"][0]
        self.assertTrue(stale["stale_read"])
        self.assertEqual(stale["lost_unattempted_now"], 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=["sample_news/50", "sample_news/51"])["ok"])

    def test_newer_attempt_between_source_and_serialization_fences_stale_ids(self):
        self.call("channels", action="add", name="sample_news")

        def page_of(n):
            return ([{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}",
                      "text": str(n), "date": "", "links": []}], "")

        original = plugin._telegram_source

        def interleave(channel, limit, include_attempted, watermark):
            old = original(channel, limit, include_attempted, watermark)
            with patch.object(plugin, "_telegram_source", original), patch.object(
                    plugin, "_fetch_channel", return_value=page_of(2)):
                newer = self.call("fetch_posts", channel=channel)["channels"][0]
            self.assertEqual(newer["posts"][0]["id"], "sample_news/2")
            self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                      post_ids=["sample_news/2"])["ok"])
            return old

        with patch.object(plugin, "_fetch_channel", return_value=page_of(1)), patch.object(
                plugin, "_telegram_source", side_effect=interleave):
            stale = self.call("fetch_posts", channel="sample_news")["channels"][0]
        self.assertTrue(stale["stale_read"])
        self.assertEqual((stale["posts"], textless(stale)), ([], []))
        self.assertEqual(stale["lost_unattempted_now"], 0)
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT id FROM observed WHERE channel='sample_news'").fetchall()[0][0], 2)

    def test_existing_receipt_survives_source_removal(self):
        self.call("channels", action="add", name="sample_news")
        posts = ([{"id": "sample_news/12", "url": "https://t.me/sample_news/12",
                   "text": "post", "date": "", "links": []}], "")
        with patch.object(plugin, "_fetch_channel", return_value=posts):
            self.call("fetch_posts")
        first = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/12"])
        self.assertTrue(first["new_attempt"])
        self.call("channels", action="remove", name="sample_news")
        again = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/12"])
        self.assertFalse(again["new_attempt"])
        self.assertTrue(again["same_selection"])
        self.assertEqual(again["status"], "already_attempted_do_not_auto_publish")

    def test_twenty_one_parsed_posts_are_not_falsely_lost(self):
        self.call("channels", action="add", name="sample_news")
        content = b"<html><body>%s</body></html>" % b"".join((
            f'<div class="tgme_widget_message" data-post="sample_news/{n}">'
            f'<div class="tgme_widget_message_text">Post {n}</div></div>').encode() for n in range(1, 22))
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=content)):
            first = self.call("fetch_posts", limit=20)["channels"][0]
            self.assertEqual(first["posts"][0]["id"], "sample_news/1")
            self.assertEqual(first["omitted_posts"], 1)
            again = self.call("fetch_posts", limit=20)["channels"][0]
        self.assertEqual(again["lost_unattempted_now"], 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[p["id"] for p in first["posts"]])["ok"])

    def test_limited_page_keeps_oldest_unattempted_and_replays_newer_next_run(self):
        self.call("channels", action="add", name="sample_news")
        sample = [{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}", "text": str(n), "date": "", "links": []}
                  for n in range(1, 21)]
        with patch.object(plugin, "_fetch_channel", return_value=(sample, "")):
            first = self.call("fetch_posts", limit=10)["channels"][0]
            self.assertEqual(first["posts"][0]["id"], "sample_news/1")
            self.assertEqual(first["posts"][-1]["id"], "sample_news/10")
            self.call("record_attempt", day="2026-09-27", post_ids=[p["id"] for p in first["posts"]])
            second = self.call("fetch_posts", limit=10)["channels"][0]
        self.assertEqual(second["posts"][0]["id"], "sample_news/11")
        self.assertEqual(second["posts"][-1]["id"], "sample_news/20")

    def test_output_budget_names_dropped_posts(self):
        self.call("channels", action="add", name="sample_news")
        sample = [{"id": f"sample_news/{n}", "date": "", "text": "x" * 1200, "url": f"https://t.me/sample_news/{n}", "links": []}
                  for n in range(1, 21)]
        with patch.object(plugin, "_fetch_channel", return_value=(sample, "")):
            result = self.call("fetch_posts", limit=20)
        self.assertTrue(result["output_limited"])
        self.assertGreater(result["channels"][0]["omitted_posts"], 0)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)

    def test_large_interest_and_attempt_state_do_not_break_json_bound(self):
        self.call("channels", action="add", name="sample_news")
        self.call("interests", text="и" * plugin.MAX_INTERESTS)
        posts = [{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}", "text": "x" * 1200, "date": "", "links": []}
                 for n in range(1, 21)]
        with patch.object(plugin, "_fetch_channel", return_value=(posts, "")):
            result = self.call("fetch_posts", limit=20)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        self.assertGreater(result["channels"][0]["omitted_posts"], 0)

    def test_many_channels_can_record_every_returned_id(self):
        for number in range(12):
            self.call("channels", action="add", name=f"channel{number}")

        def page_of(channel):
            return ([{"id": f"{channel}/{n}", "url": f"https://t.me/{channel}/{n}",
                      "text": "x", "date": "", "links": []} for n in range(1, 21)], "")

        with patch.object(plugin, "_fetch_channel", side_effect=page_of):
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

        def page_of(channel):
            return ([{"id": f"{channel}/1", "url": f"https://t.me/{channel}/1",
                      "text": "x" * 1200, "date": "", "links": ["https://example.org/" + "z" * 280] * 3}], "")

        with patch.object(plugin, "_fetch_channel", side_effect=page_of):
            result = self.call("fetch_posts", limit=20)
        self.assertTrue(result["output_limited"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        deferred = [row for row in result["channels"] if row["coverage"] == "output_deferred"]
        self.assertTrue(deferred)
        self.assertTrue(all(row["first_omitted_id"] == f"{row['channel']}/1" for row in deferred))
        with patch.object(plugin, "_fetch_channel", side_effect=page_of):
            recovered = self.call("fetch_posts", channel=deferred[0]["channel"])["channels"][0]
        self.assertEqual(recovered["coverage"], "recent_page_only")
        self.assertEqual(recovered["posts"][0]["id"], deferred[0]["first_omitted_id"])

    def test_escaped_interest_text_cannot_blow_output_cap(self):
        self.assertFalse(self.call("interests", text="a\n" * 2000)["ok"])
        self.assertEqual(self.call("interests")["interests"], "")

    def test_fresh_install_is_neutral_and_asks_owner_instead_of_assuming(self):
        # Nothing is preconfigured: no interests, sources or schedule.
        fresh = self.call("interests")
        self.assertEqual((fresh["interests"], fresh["setup_needed"]), ("", ["interests", "sources"]))
        self.assertIn("own words", fresh["onboarding"])
        self.assertNotIn("guidance", fresh)  # the skill neither schedules nor proposes schedules
        listed = self.call("channels")
        self.assertEqual((listed["channels"], listed["setup_needed"]), ([], ["interests", "sources"]))
        unconfigured = self.call("fetch_posts")
        self.assertFalse(unconfigured["ok"])
        self.assertEqual(unconfigured["setup_needed"], ["interests", "sources"])
        # A blank argument reads instead of failing or wiping the owner's text.
        self.call("interests", text="Owner wording, kept verbatim")
        for blank in ("", "   \n"):
            with self.subTest(blank=blank):
                kept = self.call("interests", text=blank)
                self.assertTrue(kept["ok"])
                self.assertEqual(kept["interests"], "Owner wording, kept verbatim")
                self.assertIn("unchanged", kept["note"])
        self.assertEqual(self.call("interests")["setup_needed"], ["sources"])
        added = self.call("channels", action="add", name="sample_news")
        self.assertEqual(added["added"], "sample_news")
        self.assertIn("fetch_posts", added["verify"])
        configured = self.call("channels")
        self.assertEqual(configured["setup_needed"], [])
        self.assertNotIn("onboarding", configured)

    def test_sources_without_interests_still_fetch_but_request_owner_interests(self):
        self.call("channels", action="add", name="sample_news")
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            result = self.call("fetch_posts")
        self.assertTrue(result["ok"])
        self.assertEqual(result["setup_needed"], ["interests"])
        self.assertIn("onboarding", result)

    def test_gzip_bomb_preview_is_refused_without_full_expansion(self):
        self.call("channels", action="add", name="sample_news")
        bomb = gzip.compress(b"<div>" + b" " * (64 * plugin.MAX_RESPONSE_BYTES))
        self.assertLess(len(bomb), 200 * 1024)
        inflated = []
        original = plugin.zlib.decompressobj

        def tracked(*args):
            real = original(*args)

            class Probe:
                eof = property(lambda _self: real.eof)

                def decompress(self, data, max_length=0):
                    out = real.decompress(data, max_length)
                    inflated.append(len(out))
                    return out
            return Probe()

        with patch.object(plugin.zlib, "decompressobj", side_effect=tracked), \
                patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=bomb, encoding="gzip")):
            source = self.call("fetch_posts")["channels"][0]
        self.assertEqual((source["coverage"], source["error"]), ("unavailable", "preview exceeds byte limit"))
        self.assertLessEqual(sum(inflated), plugin.MAX_RESPONSE_BYTES + 1)


class TextNotExtractedTests(ConsumerCase):
    """Owner decision of 2026-11-01: a partial digest with explicit gaps that still advances."""

    def setUp(self):
        super().setUp()
        self.call("channels", action="add", name="sample_news")

    def test_mixed_page_returns_readable_posts_and_lists_the_rest_separately(self):
        content = page(post_html(20, "First readable"), post_html(21, extra=PHOTO),
                       post_html(22, "Second <b>readable</b>"), post_html(23, extra=PLACEHOLDER),
                       post_html(24, "Readable with an attachment the preview cannot show", extra=PLACEHOLDER))
        result = self.fetch_page(content)
        source = result["channels"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(source["coverage"], "recent_page_only")
        self.assertEqual([(p["id"], p["text"]) for p in source["posts"]],
                         [("sample_news/20", "First readable"), ("sample_news/22", "Second readable"),
                          ("sample_news/24", "Readable with an attachment the preview cannot show")])
        self.assertEqual(source["text_not_extracted"], [
            {"id": "sample_news/21", "url": "https://t.me/sample_news/21", "date": stamp(1)},
            {"id": "sample_news/23", "url": "https://t.me/sample_news/23", "date": stamp(1),
             "preview_note": "Please open Telegram to view this post"}])
        self.assertNotIn("Please open Telegram", json.dumps(source["posts"]))
        self.assertNotIn("media_only", json.dumps(result))
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=[p["id"] for p in source["posts"]])
        self.assertEqual((receipt["status"], receipt["post_ids"]),
                         ("attempted_not_delivered", ["sample_news/20", "sample_news/22", "sample_news/24"]))
        self.assertEqual(receipt["attempted_not_read"], {"count": 2, "ids": ["sample_news/21", "sample_news/23"]})
        self.assertEqual(self.watermark(), 24)
        again = self.fetch_page(content)["channels"][0]
        self.assertEqual((again["posts"], textless(again), again["possible_gap"]), ([], [], False))
        # A separately requested retry brings the attempted-not-read posts back while they are still public.
        retry = self.fetch_page(content, include_attempted=True)["channels"][0]
        self.assertEqual(retry["posts"], [])
        self.assertEqual([p["id"] for p in retry["text_not_extracted"]], ["sample_news/21", "sample_news/23"])
        self.assertTrue(all(p["already_attempted"] for p in retry["text_not_extracted"]))

    def test_portion_of_only_text_not_extracted_posts_advances_without_model_ids(self):
        first = [post_html(30, "Readable"), post_html(31, extra=PHOTO), post_html(32, extra=PHOTO)]
        fetched = self.fetch_page(page(*first))
        source = fetched["channels"][0]
        self.assertEqual([p["id"] for p in source["text_not_extracted"]], ["sample_news/31", "sample_news/32"])
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=[fetched["fetch_id"]])
        self.assertEqual(receipt["post_ids"], ["sample_news/30", "sample_news/31", "sample_news/32"])
        self.assertEqual(receipt["attempted_not_read"]["ids"], ["sample_news/31", "sample_news/32"])
        later = page(*first, post_html(33, extra=PHOTO), post_html(34, extra=PLACEHOLDER))
        result = self.fetch_page(later)
        source = result["channels"][0]
        self.assertTrue(result["ok"])  # the page is readable; only its new portion has no text
        self.assertEqual((source["posts"], [p["id"] for p in source["text_not_extracted"]]),
                         ([], ["sample_news/33", "sample_news/34"]))
        receipt = self.call("record_attempt", day="2026-09-28", fetch_ids=[result["fetch_id"]])
        self.assertEqual(receipt["attempted_not_read"], {"count": 2, "ids": ["sample_news/33", "sample_news/34"]})
        self.assertEqual(self.watermark(), 34)
        self.assertEqual(textless(self.fetch_page(later)["channels"][0]), [])

    def test_post_ids_record_what_they_name_and_never_skip_a_returned_readable_post(self):
        content = page(post_html(40, "A"), post_html(41, extra=PHOTO), post_html(42, "B"), post_html(43, extra=PHOTO))
        self.fetch_page(content)
        refused = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/42"])
        self.assertIn("every earlier returned post", refused["error"])
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/40"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 0, "ids": []})
        self.assertEqual(self.watermark(), 40)
        source = self.fetch_page(content)["channels"][0]
        self.assertEqual(([p["id"] for p in source["posts"]], [p["id"] for p in source["text_not_extracted"]]),
                         (["sample_news/42"], ["sample_news/41", "sample_news/43"]))
        # A textless post the cursor passes on the way to a named one was attempted, not read.
        receipt = self.call("record_attempt", day="2026-09-28", post_ids=["sample_news/42"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 1, "ids": ["sample_news/41"]})
        self.assertEqual(self.watermark(), 42)

    def test_page_without_any_extracted_text_is_unavailable_and_not_recordable(self):
        cases = {"photo only": page(post_html(22, extra=PHOTO)),
                 "placeholders and photos": page(post_html(22, extra=PLACEHOLDER), post_html(23, extra=PHOTO)),
                 "changed text markup": page(post_html(22, "News after markup drift", text_class="message_body")),
                 "empty text block": page(post_html(22, "   "))}
        for edition, (label, content) in enumerate(cases.items()):
            with self.subTest(label):
                result = self.fetch_page(content)
                source = result["channels"][0]
                self.assertFalse(result["ok"])
                self.assertEqual((source["coverage"], source["posts"], textless(source)),
                                 ("unavailable", [], []))
                self.assertIn("text not extracted for any of the", source["error"])
                self.assertFalse(self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/22"])["ok"])
                self.assertTrue(self.call("record_attempt", day="2026-09-27", edition=f"e{edition}", post_ids=[])["ok"])
                self.assertEqual(self.watermark(), 0)
        readable = self.fetch_page(page(post_html(22, "Text is back")))["channels"][0]
        self.assertEqual([p["id"] for p in readable["posts"]], ["sample_news/22"])

    def test_telegram_placeholder_is_never_post_text(self):
        content = page(post_html(50, PLACEHOLDER), post_html(51, "Own words " + PLACEHOLDER + " continue"),
                       post_html(52, "Plain"))
        source = self.fetch_page(content)["channels"][0]
        self.assertEqual([(p["id"], p["text"]) for p in source["posts"]],
                         [("sample_news/51", "Own words continue"), ("sample_news/52", "Plain")])
        self.assertEqual(source["text_not_extracted"], [
            {"id": "sample_news/50", "url": "https://t.me/sample_news/50", "date": stamp(1),
             "preview_note": "Please open Telegram to view this post"}])

    def test_first_fetch_window_counts_old_posts_as_baseline_that_never_blocks(self):
        content = page(post_html(1, "Months old", hours_ago=24 * 90), post_html(2, "Ten days old", hours_ago=240),
                       post_html(3, extra=PHOTO, hours_ago=200), post_html(4, "Fresh", hours_ago=30),
                       post_html(5, extra=PHOTO, hours_ago=2), post_html(6, "Undated", dated=False))
        source = self.fetch_page(content)["channels"][0]
        self.assertEqual(source["window_since"], (NOW - timedelta(days=7)).isoformat())
        self.assertEqual(source["baseline_posts"], 3)
        self.assertEqual(([p["id"] for p in source["posts"]], [p["id"] for p in source["text_not_extracted"]]),
                         (["sample_news/4", "sample_news/6"], ["sample_news/5"]))
        self.assertFalse(source["possible_gap"])
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/4", "sample_news/6"])
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.watermark(), 6)
        after = self.fetch_page(content)["channels"][0]
        self.assertEqual((after["posts"], textless(after), after.get("baseline_posts", 0)), ([], [], 0))
        self.assertNotIn("window_since", after)  # the first-run window no longer applies after an attempt
        newer = self.fetch_page(page(post_html(6, "Undated", dated=False), post_html(7, "Next", hours_ago=0)))
        self.assertEqual([p["id"] for p in newer["channels"][0]["posts"]], ["sample_news/7"])

    def test_returned_post_never_becomes_a_hidden_baseline_that_blocks_recording(self):
        content = page(post_html(1, "Returned once, never recorded", hours_ago=48), post_html(2, "Old", hours_ago=24 * 9))
        self.assertEqual([p["id"] for p in self.fetch_page(content)["channels"][0]["posts"]], ["sample_news/1"])
        self.call("channels", action="remove", name="sample_news")
        self.utcnow.return_value = NOW + timedelta(days=30)
        self.call("channels", action="add", name="sample_news")  # the window moves forward to the re-add
        source = self.fetch_page(content)["channels"][0]
        self.assertEqual(([p["id"] for p in source["posts"]], source["baseline_posts"]), (["sample_news/1"], 1))
        self.assertTrue(self.call("record_attempt", day="2026-10-27", post_ids=["sample_news/1"])["ok"])

    def test_quiet_channel_whose_page_is_all_baseline_returns_nothing_new(self):
        old = page(post_html(8, "Old", hours_ago=24 * 30), post_html(9, "Older news", hours_ago=24 * 20))
        result = self.fetch_page(old)
        source = result["channels"][0]
        self.assertTrue(result["ok"])
        self.assertEqual((source["posts"], source["baseline_posts"], source["omitted_posts"]), ([], 2, 0))
        fresh = self.fetch_page(page(post_html(9, "Older news", hours_ago=24 * 20), post_html(10, "New", hours_ago=3)))
        self.assertEqual([p["id"] for p in fresh["channels"][0]["posts"]], ["sample_news/10"])
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/10"])["ok"])

    def test_remaining_portion_is_reported_and_continues_next_edition(self):
        content = page(*[post_html(n, extra=PHOTO) if n == 13 else post_html(n, f"Post {n}") for n in range(1, 31)])
        first = self.fetch_page(content, limit=10)["channels"][0]
        self.assertEqual([p["id"] for p in first["posts"]], [f"sample_news/{n}" for n in range(1, 11)])
        self.assertEqual((first["omitted_posts"], first["first_omitted_id"]), (20, "sample_news/11"))
        self.call("record_attempt", day="2026-09-27", post_ids=[p["id"] for p in first["posts"]])
        second = self.fetch_page(content, limit=10)["channels"][0]
        self.assertEqual([p["id"] for p in second["posts"]],
                         [f"sample_news/{n}" for n in (11, 12, 14, 15, 16, 17, 18, 19, 20, 21)])
        self.assertEqual([p["id"] for p in second["text_not_extracted"]], ["sample_news/13"])
        self.assertEqual((second["omitted_posts"], second["first_omitted_id"]), (9, "sample_news/22"))
        receipt = self.call("record_attempt", day="2026-09-28", post_ids=[p["id"] for p in second["posts"]])
        self.assertEqual(receipt["attempted_not_read"]["ids"], ["sample_news/13"])
        third = self.fetch_page(content, limit=10)["channels"][0]
        self.assertEqual(([p["id"] for p in third["posts"]], third["omitted_posts"]),
                         ([f"sample_news/{n}" for n in range(22, 31)], 0))

    def test_output_trimming_keeps_returned_entries_one_contiguous_run(self):
        posts = [post_html(n, extra=PHOTO) if n in (7, 9) else post_html(n, "y" * 900) for n in range(1, 10)]
        content = page(*posts)
        with patch.object(plugin, "MAX_OUTPUT_CHARS", 4500):
            result = self.fetch_page(content, limit=20)
            source = result["channels"][0]
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 4500)
            returned = [p["id"] for p in source["posts"] + textless(source)]
            boundary = int(source["first_omitted_id"].split("/")[1])
            self.assertTrue(all(int(post_id.split("/")[1]) < boundary for post_id in returned))
            self.assertEqual(len(returned) + source["omitted_posts"], 9)
            receipt = self.call("record_attempt", day="2026-09-27", post_ids=[p["id"] for p in source["posts"]])
            self.assertLess(self.watermark(), boundary)
            self.assertTrue(all(int(i.split("/")[1]) < boundary for i in receipt["attempted_not_read"]["ids"]))
            following = self.fetch_page(content, limit=20)["channels"][0]
        entries = following["posts"] + textless(following)
        self.assertEqual(min(int(p["id"].split("/")[1]) for p in entries), boundary)

    def test_source_keeping_only_a_textless_link_is_not_output_deferred(self):
        content = page(post_html(1, extra=PHOTO), post_html(2, "x" * 3000), post_html(3, "y" * 3000))
        with patch.object(plugin, "MAX_OUTPUT_CHARS", 2500):
            kept = self.fetch_page(content)
        source = kept["channels"][0]
        # The budget moved both readable posts to the next portion; the link it kept is an entry.
        self.assertEqual((source["coverage"], source["posts"], [p["id"] for p in textless(source)]),
                         ("recent_page_only", [], ["sample_news/1"]))
        self.assertEqual((source["omitted_posts"], source["first_omitted_id"]), (2, "sample_news/2"))
        # One character less and the link has to wait as well: only then is the source deferred.
        with patch.object(plugin, "MAX_OUTPUT_CHARS", len(json.dumps(kept, ensure_ascii=False)) - 1):
            deferred = self.fetch_page(content)["channels"][0]
        self.assertEqual((deferred["coverage"], deferred["posts"], textless(deferred), deferred["omitted_posts"]),
                         ("output_deferred", [], [], 3))

    def test_many_text_not_extracted_lists_fit_the_output_bound(self):
        names = [f"channel{number}" for number in range(11)]
        for name in names:
            self.call("channels", action="add", name=name)

        def page_of(channel):
            return ([{"id": f"{channel}/1", "url": f"https://t.me/{channel}/1", "text": "readable", "date": "",
                      "links": []}] + [{"id": f"{channel}/{n}", "url": f"https://t.me/{channel}/{n}", "text": "",
                                        "date": stamp(1), "links": []} for n in range(2, 40)], "")

        with patch.object(plugin, "_fetch_channel", side_effect=page_of):
            result = self.call("fetch_posts", limit=20)
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        self.assertTrue(result["output_limited"])
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=[result["fetch_id"]])
        self.assertTrue(receipt["ok"])
        self.assertGreater(receipt["attempted_not_read"]["count"], 0)

    def test_telegram_redirect_is_reported_not_followed(self):
        error = plugin.urllib.error.HTTPError("https://t.me/s/sample_news", 302, "Found",
                                              {"Location": "https://t.me/sample_news"}, None)
        with patch.object(plugin._OPENER, "open", side_effect=error) as opened:
            result = self.call("fetch_posts")
        opened.assert_called_once()
        source = result["channels"][0]
        self.assertFalse(result["ok"])
        self.assertEqual((source["coverage"], source["error"], source["redirect_to"]),
                         ("redirected", "HTTP 302: redirect not followed", "https://t.me/sample_news"))
        self.assertEqual(self.call("channels")["channels"], ["sample_news"])

    def test_non_public_telegram_address_is_refused_before_connecting(self):
        self.resolve.return_value = ["127.0.0.1"]
        with patch.object(plugin._OPENER, "open") as opened:
            source = self.call("fetch_posts")["channels"][0]
        opened.assert_not_called()
        self.assertEqual((source["coverage"], source["error"]),
                         ("unavailable", "preview unavailable: refused: t.me resolves to non-public address 127.0.0.1"))

    def test_legacy_rows_migrate_without_losing_ids(self):
        path = Path(self.api.root) / "digest.sqlite3"
        path.unlink()
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE observed (channel TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(channel,id))")
            db.execute("INSERT INTO observed VALUES ('sample_news',12)")
            db.execute("CREATE TABLE telegram_reads (channel TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO telegram_reads VALUES ('sample_news',3)")
            db.execute("CREATE TABLE feeds (name TEXT PRIMARY KEY, since TEXT NOT NULL, "
                       "read_generation INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO feeds VALUES ('rss:openai_news','2026-09-01T00:00:00+00:00',0)")
            db.execute("CREATE TABLE channels (name TEXT PRIMARY KEY COLLATE NOCASE)")
            db.execute("INSERT INTO channels VALUES ('sample_news'),('rss:openai_news')")
            db.execute("CREATE TABLE gaps (channel TEXT PRIMARY KEY, lost_count INTEGER NOT NULL)")
            db.execute("INSERT INTO gaps VALUES ('rss:openai_news',3)")
        listed = self.call("channels")
        self.assertEqual(listed["feed_urls"], {"rss:openai_news": ""})
        with sqlite3.connect(path) as db:
            columns = {table: {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                       for table in ("observed", "telegram_reads", "feeds")}
            self.assertEqual(db.execute("SELECT channel,lost_count,dropped_count FROM gaps").fetchall(),
                             [("rss:openai_news", 3, 0)])
            self.assertTrue({"lost_reported", "text_extracted"} <= columns["observed"])
            self.assertIn("since", columns["telegram_reads"])
            self.assertIn("url", columns["feeds"])
            self.assertEqual(db.execute("SELECT id,text_extracted FROM observed").fetchall(), [(12, 1)])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            result = self.call("fetch_posts")
        legacy = next(row for row in result["channels"] if row["channel"] == "rss:openai_news")
        self.assertEqual((legacy["coverage"], legacy["historical_lost_unattempted"]), ("unavailable", 3))
        self.assertIn("add its https:// URL", legacy["error"])
        self.assertEqual(next(row for row in result["channels"] if row["channel"] == "sample_news")["coverage"],
                         "recent_page_only")
        with sqlite3.connect(path) as db:  # a channel an older revision added gets its window at its first read
            self.assertEqual(db.execute("SELECT since FROM telegram_reads").fetchall(),
                             [((NOW - timedelta(days=7)).isoformat(),)])
        self.assertEqual(self.call("channels", action="remove", name="rss:openai_news")["channels"], ["sample_news"])


OPENAI = "https://openai.com/news/rss.xml"
DEEPMIND = "https://deepmind.google/blog/rss.xml"
SGLANG = "https://github.com/sgl-project/sglang/releases.atom"
NATURE = "https://www.nature.com/nature.rss"
SCIENCE = "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science"
ARXIV = "https://rss.arxiv.org/atom/cs.CL"


def key(url):
    return plugin._source_name(url)[0]


def feed_id(guid, url=OPENAI):
    return f"{key(url)}/" + hashlib.sha256(guid.encode()).hexdigest()[:20]


def rss(*items):
    """items: (guid, hours_ago | None, title[, description])."""
    rows = []
    for guid, hours_ago, title, *rest in items:
        stamp_tag = "" if hours_ago is None else "<pubDate>%s</pubDate>" % email.utils.format_datetime(
            NOW - timedelta(hours=hours_ago), usegmt=True)
        link = "<link>%s</link>" % guid if guid.startswith("https://") else ""
        guid_tag = "<guid>%s</guid>" % guid if guid else ""
        rows.append("<item><title><![CDATA[%s]]></title><description><![CDATA[%s]]></description>%s%s%s</item>"
                    % (title, rest[0] if rest else "summary of " + title, link, guid_tag, stamp_tag))
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>OpenAI News</title>'
            '<link>https://openai.com/news</link>%s</channel></rss>' % "".join(rows)).encode()


def rdf(*items, prefix=""):
    """RSS 1.0 as Nature, Science and bioRxiv serve it.
    items: (about, dc_date | None, title, description | None[, content_encoded])."""
    p = f"{prefix}:" if prefix else ""
    namespace = f'xmlns:{prefix}="{plugin.RSS1}"' if prefix else f'xmlns="{plugin.RSS1}"'
    rows = []
    for about, dc_date, title, description, *content in items:
        rows.append(f'<{p}item rdf:about="{about}"><{p}title><![CDATA[{title}]]></{p}title>'
                    f'<{p}link> {about} </{p}link>'
                    + (f"<{p}description><![CDATA[{description}]]></{p}description>" if description is not None else "")
                    + (f"<content:encoded><![CDATA[{content[0]}]]></content:encoded>" if content else "")
                    + (f"<dc:date>{dc_date}</dc:date>" if dc_date else "")
                    + f"<dc:identifier>doi:10.1/{len(rows)}</dc:identifier></{p}item>")
    return (f'<?xml version="1.0" encoding="UTF-8"?><rdf:RDF xmlns:rdf="{plugin.RDF}" {namespace} '
            f'xmlns:dc="{plugin.DC}" xmlns:content="{plugin.CONTENT}">'
            f'<{p}channel rdf:about="https://journal.example/rss"><{p}title>Journal</{p}title><{p}items><rdf:Seq>'
            + "".join(f'<rdf:li rdf:resource="{item[0]}"/>' for item in items)
            + f"</rdf:Seq></{p}items></{p}channel>" + "".join(rows) + "</rdf:RDF>").encode()


def article(slug):
    return f"https://openai.com/index/{slug}"


def feed_response(content, **kwargs):
    kwargs.setdefault("url", OPENAI)
    kwargs.setdefault("content_type", "text/xml; charset=utf-8")
    return FakeResponse(content=content, **kwargs)


def atom(*entries):
    """GitHub releases.atom shape. entries: (tag, hours_ago | None, title[, html_notes])."""
    rows = []
    for tag, hours_ago, title, *rest in entries:
        stamp_tag = "" if hours_ago is None else "<updated>%s</updated>" % (
            NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append('<entry><id>tag:github.com,2008:Repository/1/%s</id>%s'
                    '<link rel="alternate" type="text/html" href="https://github.com/sgl-project/sglang/releases/tag/%s"/>'
                    '<title>%s</title><content type="html">%s</content><author><name>maintainer</name></author>'
                    '<media:thumbnail height="30" width="30" url="https://avatars.githubusercontent.com/u/1?s=60&amp;v=4"/>'
                    '</entry>' % (tag, stamp_tag, tag, title, html.escape(rest[0] if rest else "<p>Notes for %s</p>" % tag)))
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:media="http://search.yahoo.com/mrss/" xml:lang="en-US">'
            '<id>tag:github.com,2008:https://github.com/sgl-project/sglang/releases</id>'
            '<link type="text/html" rel="alternate" href="https://github.com/sgl-project/sglang/releases"/>'
            '<link type="application/atom+xml" rel="self" href="%s"/><title>Release notes from sglang</title>%s</feed>'
            % (SGLANG, "".join(rows))).encode()


def release_id(tag):
    return feed_id(f"tag:github.com,2008:Repository/1/{tag}", SGLANG)


def atom_response(content, url=SGLANG):
    return feed_response(content, url=url, content_type="application/atom+xml; charset=utf-8")


def route(pages):
    def opened(request, timeout):
        response = pages[request.full_url]
        if isinstance(response, Exception):
            raise response
        return response
    return opened


class FeedSourceTests(ConsumerCase):
    def fetch(self, pages, **kwargs):
        with patch.object(plugin._OPENER, "open", side_effect=route(pages)) as opened:
            result = self.call("fetch_posts", **kwargs)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
        return result, [call.args[0].full_url for call in opened.call_args_list]

    def source(self, result, url=OPENAI):
        return next(row for row in result["channels"] if row["channel"] == key(url))

    def add(self, url):
        added = self.call("channels", action="add", name=url)
        self.assertTrue(added["ok"], added)
        return added

    def test_owner_chosen_https_feeds_are_validated_when_added(self):
        nature = self.add(NATURE)
        self.assertRegex(key(NATURE), r"\Arss:nature_com_[0-9a-f]{8}\Z")
        self.assertEqual(key(NATURE), "rss:nature_com_" + hashlib.sha256(NATURE.encode()).hexdigest()[:8])
        self.assertEqual((nature["added"], nature["feed_urls"]), (key(NATURE), {key(NATURE): NATURE}))
        self.assertIn("fetch_posts", nature["verify"])
        normalized = {
            "https://пример.рф/лента?x=1": "https://xn--e1afmkfd.xn--p1ai/%D0%BB%D0%B5%D0%BD%D1%82%D0%B0?x=1",
            "HTTPS://Export.Example.ORG./a%20b?q=1&r=2": "https://export.example.org/a%20b?q=1&r=2",
            "https://8.8.8.8:8443/feed": "https://8.8.8.8:8443/feed",
            SCIENCE: SCIENCE,
        }
        for raw, url in normalized.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.add(raw)["feed_urls"][key(url)], url)
        refused = {
            "https://a..b/feed": "is not a valid domain name",
            "https://localhost/feed": "is not a valid domain name",
            "https://-bad.example/feed": "is not a valid domain name",
            "https://under_score.example/feed": "is not a valid domain name",
            "https:///feed": "is not a valid domain name",
            "http://example.org/feed": "must use https://",
            "ftp://example.org/feed": "must use https://",
            "https://user:secret@example.org/feed": "user name or password",
            "https://example.org/feed#latest": "#fragment",
            "https://exa mple.org/feed": "without spaces",
            "https://example.org/" + "a" * plugin.MAX_URL: "at most",
            "https://example.org:99999/feed": "malformed",
            "https://127.0.0.1/feed": "not a public address",
            "https://[::1]/feed": "not a public address",
            "https://10.0.0.1/rss": "not a public address",
            "rss:unknown_feed": "by its https:// URL",
        }
        with patch.object(plugin._OPENER, "open") as opened:
            for raw, reason in refused.items():
                with self.subTest(raw=raw[:60]):
                    answer = self.call("channels", action="add", name=raw)
                    self.assertFalse(answer["ok"])
                    self.assertIn(reason, answer["error"])
        opened.assert_not_called()
        self.resolve.assert_not_called()  # validation at add time never touches the network
        self.assertEqual(self.call("channels", action="remove", name=NATURE)["channels"].count(key(NATURE)), 0)
        self.assertNotIn(key(SCIENCE), self.call("channels", action="remove", name=key(SCIENCE))["channels"])
        self.assertFalse(self.call("fetch_posts", channel="https://example.org/never-added.xml")["ok"])

    def test_deepmind_feed_shape_is_its_own_recordable_source(self):
        self.assertEqual(self.add(DEEPMIND)["channels"], [key(DEEPMIND)])
        post = "https://deepmind.google/blog/introducing-a-model/"
        # Real shape: namespaced media/atom elements, an empty description, text/xml without charset.
        feed = ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
                'xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Google DeepMind News</title>'
                '<atom:link href="https://example.com/blog/rss.xml" rel="self"/><item><title>Introducing a model</title>'
                '<link>%s</link><description/><pubDate>%s</pubDate><guid>%s</guid>'
                '<media:thumbnail url="https://lh3.googleusercontent.com/x"/></item></channel></rss>'
                % (post, email.utils.format_datetime(NOW - timedelta(hours=3)), post)).encode()
        response = feed_response(feed, url=DEEPMIND, content_type="text/xml")
        source = self.source(self.fetch({DEEPMIND: response})[0], DEEPMIND)
        self.assertEqual((source["coverage"], source["format"]), ("feed_window", "rss2.0"))
        [item] = source["posts"]
        self.assertEqual((item["id"], item["url"], item["title"], item["excerpt"]),
                         (feed_id(post, DEEPMIND), post, "Introducing a model", ""))
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=[item["id"]])["ok"])
        self.assertEqual(self.source(self.fetch({DEEPMIND: response})[0], DEEPMIND)["posts"], [])

    def test_mixed_fetch_returns_bounded_cited_records_and_attempt_is_not_delivery(self):
        self.call("channels", action="add", name="sample_news")
        self.add(OPENAI)
        feed = rss((article("fresh"), 5, "Fresh model card", "<p>System card &amp; <b>evals</b></p>"),
                   (article("older"), 30, "Older post"),
                   (article("launch-2015"), 24 * 365 * 10, "Archive item"))
        pages = {"https://t.me/s/sample_news": FakeResponse(), OPENAI: feed_response(feed)}
        result, urls = self.fetch(pages, limit=5)
        self.assertEqual(sorted(urls), sorted(pages))
        self.assertTrue(result["ok"])
        telegram, source = next(r for r in result["channels"] if r["channel"] == "sample_news"), self.source(result)
        self.assertEqual((telegram["kind"], source["kind"]), ("telegram", "rss"))
        self.assertEqual(source["coverage"], "feed_window")
        # Oldest first; the publisher archive before the subscription window is not a backlog.
        self.assertEqual([p["id"] for p in source["posts"]], [feed_id(article("older")), feed_id(article("fresh"))])
        fresh = source["posts"][1]
        self.assertEqual(fresh["url"], article("fresh"))
        self.assertEqual(fresh["title"], "Fresh model card")
        self.assertEqual(fresh["excerpt"], "System card & evals")
        self.assertNotIn("text", fresh)  # a feed excerpt is never presented as read text
        self.assertEqual(fresh["date"], (NOW - timedelta(hours=5)).isoformat())
        self.assertFalse(source["possible_gap"])
        self.assertEqual(source["window_since"], (NOW - timedelta(days=7)).isoformat())
        self.assertIn("not the article", result["workflow"])
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
        self.call("channels", action="add", name="sample_news")
        self.add(OPENAI)
        feed = feed_response(rss((article("a"), 2, "A")))
        refused = plugin.urllib.error.HTTPError(OPENAI, 403, "Forbidden", {}, None)
        for pages, failed, readable in (
                ({"https://t.me/s/sample_news": FakeResponse(), OPENAI: refused}, key(OPENAI), "sample_news"),
                ({"https://t.me/s/sample_news": TimeoutError(), OPENAI: feed}, "sample_news", key(OPENAI))):
            with self.subTest(failed=failed):
                result, _ = self.fetch(pages)
                rows = {row["channel"]: row for row in result["channels"]}
                self.assertTrue(result["ok"])
                self.assertEqual(rows[failed]["coverage"], "unavailable")
                self.assertIn("unavailable", rows[failed]["error"])
                self.assertTrue(rows[readable]["posts"])
        both_down = {"https://t.me/s/sample_news": TimeoutError(), OPENAI: refused}
        down, _ = self.fetch(both_down)
        self.assertFalse(down["ok"])
        self.assertEqual(self.source(down)["error"], "feed unavailable: HTTP 403")
        self.assertEqual(next(r for r in down["channels"] if r["channel"] == "sample_news")["error"],
                         "preview unavailable: TimeoutError")

    def test_any_exception_in_one_source_is_that_sources_status(self):
        self.call("channels", action="add", name="sample_news")
        self.add(OPENAI)
        self.add(DEEPMIND)
        # The reviewer's https://a..b/feed killed every source through pool.map; any such escape stays local.
        crash = UnicodeEncodeError("idna", "a..b", 0, 1, "label empty or too long")
        pages = {"https://t.me/s/sample_news": FakeResponse(), OPENAI: crash,
                 DEEPMIND: feed_response(rss(("https://deepmind.google/blog/ok/", 2, "Fine")), url=DEEPMIND)}
        result, _ = self.fetch(pages)
        rows = {row["channel"]: row for row in result["channels"]}
        self.assertTrue(result["ok"])
        self.assertEqual(rows[key(OPENAI)]["coverage"], "unavailable")
        self.assertEqual(rows[key(OPENAI)]["error"],
                         "source failed: UnicodeEncodeError: 'idna' codec can't encode character '\\x61' in "
                         "position 0: label empty or too long")
        self.assertEqual(rows[key(DEEPMIND)]["coverage"], "feed_window")
        self.assertTrue(rows["sample_news"]["posts"])
        with patch.object(plugin, "_telegram_source", side_effect=sqlite3.OperationalError("database is locked")):
            locked, _ = self.fetch(pages)
        self.assertEqual(next(r for r in locked["channels"] if r["channel"] == "sample_news")["error"],
                         "source failed: OperationalError: database is locked")
        self.assertEqual(self.source(locked, DEEPMIND)["coverage"], "feed_window")

    def test_feed_redirect_is_reported_with_a_bounded_sanitized_target(self):
        self.add(OPENAI)
        location = "//user:secret@mirror.example.org/new/feed.xml?x=1&pad=" + "a" * 400 + "#section"
        moved = plugin.urllib.error.HTTPError(OPENAI, 301, "Moved", {"Location": location}, None)
        result, urls = self.fetch({OPENAI: moved})
        source = self.source(result)
        self.assertEqual(urls, [OPENAI])  # never followed
        self.assertEqual((source["coverage"], source["error"]), ("redirected", "HTTP 301: redirect not followed"))
        target = source["redirect_to"]
        self.assertTrue(target.startswith("https://mirror.example.org/new/feed.xml?x=1&pad=aaa"))
        self.assertEqual(len(target), plugin.MAX_REDIRECT_TARGET)
        self.assertTrue(target.endswith("…"))
        self.assertNotIn("secret", target)
        self.assertNotIn("#", target)
        self.assertEqual(self.call("channels")["feed_urls"], {key(OPENAI): OPENAI})  # the source is unchanged
        for location, expected in ((None, ""), ("javascript:alert(1)", ""), ("/moved\r\nX: y", "https://openai.com/movedX:y")):
            with self.subTest(location=location):
                odd = plugin.urllib.error.HTTPError(OPENAI, 308, "x", {"Location": location} if location else {}, None)
                self.assertEqual(self.source(self.fetch({OPENAI: odd})[0])["redirect_to"], expected)

    def test_non_public_feed_address_is_refused_before_connecting(self):
        self.add(OPENAI)
        for addresses in (["10.0.0.7"], ["127.0.0.1"], ["::1"], ["93.184.215.14", "192.168.1.5"],
                          ["::ffff:10.0.0.1"], ["64:ff9b::a00:1"], ["169.254.169.254"], ["fd00::5"], ["0.0.0.0"]):
            with self.subTest(addresses=addresses):
                self.resolve.return_value = addresses
                result, urls = self.fetch({})
                source = self.source(result)
                self.assertEqual(urls, [])
                self.assertEqual(source["coverage"], "unavailable")
                self.assertEqual(source["error"], "feed unavailable: refused: openai.com resolves to non-public "
                                                  f"address {[a for a in addresses if a != '93.184.215.14'][0]}")
        self.resolve.return_value = []
        self.assertEqual(self.source(self.fetch({})[0])["error"], "feed unavailable: host lookup returned no address")
        self.resolve.return_value = None
        self.resolve.side_effect = socket.gaierror(8, "nodename nor servname provided")
        failed = self.source(self.fetch({})[0])
        self.assertEqual(failed["error"], "feed unavailable: host lookup failed: gaierror")

    def test_rss1_rdf_feeds_parse_with_their_namespaces(self):
        self.add(NATURE)
        nature = rdf(("https://www.nature.com/articles/d41586-026-1", "2026-09-26", "Briefing: <b>Dog years</b>", None,
                      '<p>Nature, Published online: 26 September 2026; <a href="https://doi.org/x">doi:10.1038/x</a></p>'
                      "Staff discuss pet longevity."),
                     ("https://www.nature.com/articles/s41586-026-2", "2026-09-25", "Rh<sup>3+</sup> catalysis", None,
                      "<p>Author Correction</p>"),
                     ("https://www.nature.com/articles/old", "2026-08-01", "Before the window", None, "old"))
        source = self.source(self.fetch({NATURE: feed_response(nature, url=NATURE,
                                                               content_type="application/rss+xml; charset=utf-8")})[0], NATURE)
        self.assertEqual((source["coverage"], source["format"], source["feed_items"]), ("feed_window", "rss1.0", 3))
        self.assertEqual([(p["id"], p["url"], p["title"], p["date"]) for p in source["posts"]], [
            (feed_id("https://www.nature.com/articles/s41586-026-2", NATURE),
             "https://www.nature.com/articles/s41586-026-2", "Rh 3+ catalysis", "2026-09-25T00:00:00+00:00"),
            (feed_id("https://www.nature.com/articles/d41586-026-1", NATURE),
             "https://www.nature.com/articles/d41586-026-1", "Briefing: Dog years", "2026-09-26T00:00:00+00:00")])
        self.assertEqual(source["posts"][1]["excerpt"],
                         "Nature, Published online: 26 September 2026; doi:10.1038/x Staff discuss pet longevity.")
        self.add(SCIENCE)
        # Science's description is only a citation rubric; it stays labelled as the feed's excerpt.
        science = rdf(("https://www.science.org/doi/abs/10.1126/science.a1?af=R", "2026-09-26T06:00:08Z",
                       "We have some explaining to do", "Science, Volume 394, Issue 6823, Page 491-491, September 2026. <br/>",
                       "Science, Volume 394"), prefix="rss")
        row = self.source(self.fetch({SCIENCE: feed_response(science, url=SCIENCE,
                                                             content_type='application/xml;")')})[0], SCIENCE)
        self.assertEqual((row["format"], row["posts"][0]["date"], row["posts"][0]["excerpt"]),
                         ("rss1.0", "2026-09-26T06:00:08+00:00", "Science, Volume 394, Issue 6823, Page 491-491, September 2026."))
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[p["id"] for p in source["posts"] + row["posts"]])["ok"])

    def test_prefixed_and_default_namespace_atom_feeds(self):
        self.add(ARXIV)
        entry = ("<{p}entry><{p}id>oai:arXiv.org:2609.0{n}</{p}id><{p}title>Paper {n}</{p}title>"
                 '<{p}link href="https://arxiv.org/abs/2609.0{n}" rel="alternate"/><{p}published>2026-09-27T00:00:00-04:00</{p}published>'
                 "<{p}summary>arXiv:2609.0{n} Abstract {n}</{p}summary></{p}entry>")
        default = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/"><id>x</id>'
                   + entry.format(p="", n=1) + "</feed>").encode()
        prefixed = ('<a:feed xmlns:a="http://www.w3.org/2005/Atom"><a:id>x</a:id>'
                    + entry.format(p="a:", n=2) + "</a:feed>").encode()
        for content, number in ((default, 1), (prefixed, 2)):
            with self.subTest(number=number):
                source = self.source(self.fetch({ARXIV: atom_response(content, ARXIV)})[0], ARXIV)
                [post] = [p for p in source["posts"] if p["title"] == f"Paper {number}"]
                self.assertEqual((source["format"], post["url"], post["excerpt"], post["date"]),
                                 ("atom", f"https://arxiv.org/abs/2609.0{number}", f"arXiv:2609.0{number} Abstract {number}",
                                  "2026-09-27T04:00:00+00:00"))
                self.assertEqual(post["id"], feed_id(f"oai:arXiv.org:2609.0{number}", ARXIV))

    def test_valid_feed_without_items_is_an_empty_list_not_an_error(self):
        self.add(ARXIV)
        cases = {  # arXiv on a Sunday: a valid Atom feed with no entries (real sample, 2026-11-01)
            "atom": ("<?xml version='1.0' encoding='UTF-8'?><feed xmlns:arxiv=\"http://arxiv.org/schemas/atom\" "
                     "xmlns=\"http://www.w3.org/2005/Atom\"><id>http://rss.arxiv.org/atom/cs.CL</id>"
                     "<title>cs.CL updates on arXiv.org</title></feed>").encode(),
            "rss2.0": rss(),
            "rss1.0": rdf(),
        }
        for label, content in cases.items():
            with self.subTest(label):
                result, _ = self.fetch({ARXIV: atom_response(content, ARXIV)})
                source = self.source(result, ARXIV)
                self.assertTrue(result["ok"])
                self.assertEqual((source["coverage"], source["error"], source["posts"], source["format"],
                                  source["feed_items"], source["possible_gap"]),
                                 ("feed_window", "", [], label, 0, False))

    def test_reordered_and_duplicated_feed_never_loses_unseen_items(self):
        self.add(OPENAI)
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
        self.add(OPENAI)
        self.fetch({OPENAI: feed_response(rss((article("a"), 5, "A"), (article("b"), 4, "B")))})
        source = self.source(self.fetch({OPENAI: feed_response(rss((article("b"), 4, "B")))})[0])
        self.assertEqual((source["lost_unattempted_now"], source["historical_lost_unattempted"]), (1, 1))
        self.assertTrue(source["possible_gap"])
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("a")), feed_id(article("b"))])["ok"])

    def test_omitted_item_that_vanishes_reports_loss_without_becoming_recordable(self):
        self.add(OPENAI)
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
        self.add(OPENAI)
        first = self.source(self.fetch({OPENAI: feed_response(rss((article("a"), 2, "A")))})[0])
        self.assertEqual([p["id"] for p in first["posts"]], [feed_id(article("a"))])
        corrected = self.source(self.fetch({OPENAI: feed_response(rss((article("a"), 20 * 24, "A")))})[0])
        self.assertLess((NOW - timedelta(hours=20 * 24)).isoformat(), corrected["window_since"])
        self.assertEqual([p["id"] for p in corrected["posts"]], [feed_id(article("a"))])
        self.assertEqual(corrected["lost_unattempted_now"], 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("a"))])["ok"])

    def test_temporarily_undated_pending_item_is_not_falsely_lost(self):
        self.add(OPENAI)
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
        self.add(OPENAI)
        duplicated = rss((article("a"), 20, "Stale"), (article("a"), 1, "Fresh"))
        source = self.source(self.fetch({OPENAI: feed_response(duplicated)})[0])
        self.assertEqual([p["id"] for p in source["posts"]], [feed_id(article("a"))])
        self.assertEqual(source["posts"][0]["title"], "Fresh")
        self.assertEqual(source["skipped_items"]["duplicate"], 1)
        self.assertTrue(self.call("record_attempt", day="2026-09-27",
                                  post_ids=[feed_id(article("a"))])["ok"])

    def test_stale_overlapping_fetch_cannot_erase_newer_returned_id(self):
        self.add(OPENAI)
        old_feed = plugin._Feed()
        old_feed.parser.Parse(rss((article("old"), 2, "Old")), True)
        old = (old_feed.items, {"feed_truncated": False}, "")
        fresh = plugin._Feed()
        fresh.parser.Parse(rss((article("new"), 1, "New")), True)
        calls = 0

        def overlapping(_url):
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
        self.add(OPENAI)
        old_feed = plugin._Feed()
        old_feed.parser.Parse(rss((article("old"), 2, "Old")), True)
        new_feed = plugin._Feed()
        new_feed.parser.Parse(rss((article("new"), 1, "New")), True)
        original_source = plugin._feed_source
        first = True

        def overlapping(source, limit):
            nonlocal first
            row = original_source(source, limit)
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
        self.add(OPENAI)
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

    def test_malformed_hostile_or_unknown_documents_are_explicit_failures(self):
        self.add(OPENAI)
        bomb = ('<!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                '<rss version="2.0"><channel><item><title>&b;</title><guid>x</guid></item></channel></rss>').encode()
        cases = {
            "not well formed": (feed_response(b"<rss><channel><item><title>x</item></channel></rss>"), "could not be parsed"),
            "entity expansion": (feed_response(bomb), "DTD or entity"),
            "html challenge": (feed_response(b"<html>Just a moment...</html>", content_type="text/html"), "HTML page"),
            "unexpected url": (feed_response(rss((article("a"), 1, "A")), url="https://openai.com/other.xml"), "unexpected URL"),
            "unknown root": (feed_response(b"<html><body>not a feed</body></html>"), "not an RSS 1.0, RSS 2.0 or Atom feed"),
            "atom without namespace": (feed_response(b"<feed><entry><id>x</id></entry></feed>"), "not an RSS 1.0"),
            "rdf that is not rss": (feed_response(('<rdf:RDF xmlns:rdf="%s"><rdf:Description rdf:about="x"/></rdf:RDF>'
                                                   % plugin.RDF).encode()), "no channel element"),
            "rss without channel": (feed_response(b'<rss version="2.0"/>'), "no channel element"),
            "no identities": (feed_response(rss(("", 1, "No guid or link"))), "lack a guid, id or link"),
            "no dates": (feed_response(rss((article("a"), None, "Undated"))), "publication dates"),
        }
        for label, (response, reason) in cases.items():
            with self.subTest(label):
                result, _ = self.fetch({OPENAI: response})
                self.assertFalse(result["ok"])
                self.assertEqual(self.source(result)["coverage"], "unavailable")
                self.assertIn(reason, self.source(result)["error"])
                self.assertEqual(self.source(result)["posts"], [])
        partial = self.source(self.fetch({OPENAI: feed_response(rss((article("a"), 1, "A"), (article("u"), None, "U")))})[0])
        self.assertEqual(partial["skipped_items"]["undated"], 1)
        self.assertEqual([p["id"] for p in partial["posts"]], [feed_id(article("a"))])

    def test_format_is_recognized_by_structure_not_by_url(self):
        self.add(SGLANG)
        served = {  # what the URL "should" serve no longer matters; the document decides
            "rss2.0": rss((article("a"), 1, "A")),
            "atom": b'<x:feed xmlns:x="http://www.w3.org/2005/Atom"><x:entry><x:id>urn:a</x:id>'
                    b"<x:updated>2026-09-27T10:00:00Z</x:updated><x:title>A</x:title></x:entry></x:feed>",
            # A bare DOCTYPE (old RSS 0.91) is inert: expat never fetches it.
            "rss2.0 ": b'<?xml version="1.0"?><!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" '
                       b'"http://my.netscape.com/publish/formats/rss-0.91.dtd"><rss version="0.91"><channel><title>T</title>'
                       b"<item><title>Old format</title><link>https://example.org/old</link>"
                       b"<pubDate>Sun, 27 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>",
        }
        for label, content in served.items():
            with self.subTest(label):
                source = self.source(self.fetch({SGLANG: atom_response(content)})[0], SGLANG)
                self.assertEqual((source["coverage"], source["format"], len(source["posts"])),
                                 ("feed_window", label.strip(), 1))

    def test_unknown_feed_encoding_is_source_local_failure(self):
        self.add(OPENAI)
        self.add(DEEPMIND)
        invalid = b'<?xml version="1.0" encoding="X-UNKNOWN"?><rss version="2.0"><channel/></rss>'
        valid = rss(("https://deepmind.google/blog/valid/", 2, "Valid"))
        result, _ = self.fetch({OPENAI: feed_response(invalid),
                                DEEPMIND: feed_response(valid, url=DEEPMIND)})
        self.assertEqual(self.source(result)["coverage"], "unavailable")
        self.assertEqual(self.source(result, DEEPMIND)["coverage"], "feed_window")

    def test_oversized_feed_keeps_complete_prefix_and_discloses_gap(self):
        self.add(OPENAI)
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
        self.add(OPENAI)
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
        self.add(OPENAI)
        pages = {OPENAI: feed_response(rss((article("a"), 1, "A", "x" * 400)))}
        normal, _ = self.fetch(pages)
        limit = len(json.dumps(normal, ensure_ascii=False)) - 1
        with patch.object(plugin, "MAX_OUTPUT_CHARS", limit):
            result, _ = self.fetch(pages)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), limit)

    def test_twelve_source_pressure_defers_feed_with_individual_recovery(self):
        for number in range(11):
            self.call("channels", action="add", name=f"channel{number}")
        self.add(OPENAI)
        self.assertIn("limit", self.call("channels", action="add", name=DEEPMIND)["error"])

        def page_of(channel):
            return ([{"id": f"{channel}/{n}", "url": f"https://t.me/{channel}/{n}", "text": "x" * 600,
                      "date": "", "links": []} for n in range(1, 21)], "")

        feed = rss(*[(article(f"p{n}"), n, f"Post {n}", "z" * 3000) for n in range(1, 21)])
        with patch.object(plugin, "_fetch_channel", side_effect=page_of), \
                patch.object(plugin._OPENER, "open", side_effect=route({OPENAI: feed_response(feed)})):
            result = self.call("fetch_posts", limit=20)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), plugin.MAX_OUTPUT_CHARS)
            self.assertTrue(result["output_limited"])
            source = self.source(result)
            self.assertEqual(source["coverage"], "output_deferred")
            self.assertEqual(source["first_omitted_id"], feed_id(article("p20")))
            ids = [p["id"] for row in result["channels"] for p in row["posts"]]
            self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=ids)["ok"])
            recovered = self.source(self.call("fetch_posts", channel=key(OPENAI), limit=3))
        self.assertEqual(recovered["coverage"], "feed_window")
        self.assertEqual(recovered["posts"][0]["id"], feed_id(article("p20")))
        self.assertEqual(recovered["omitted_posts"], 17)

    def test_revision_readd_and_bounded_state_do_not_replay_attempts(self):
        self.add(OPENAI)
        feed = feed_response(rss((article("a"), 50, "A"), (article("b"), 30, "B"), (article("c"), 10, "C")))
        ids = [p["id"] for p in self.source(self.fetch({OPENAI: feed})[0])["posts"]]
        with patch.object(plugin, "MAX_FEED_STATE", 2):
            self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=ids)["ok"])
        # A was pruned with the window moving past it; B and C are still attempted.
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feed_items WHERE attempted=1").fetchone()[0], 2)
        # A feed item has no attempted-not-read state: a retry replays none of them.
        self.assertEqual(self.source(self.fetch({OPENAI: feed}, include_attempted=True)[0])["posts"], [])
        self.call("channels", action="remove", name=OPENAI)
        self.add(OPENAI)  # the same URL maps to the same key and keeps its attempted identities
        self.assertEqual(self.source(self.fetch({OPENAI: feed})[0])["posts"], [])

    def test_truncated_ordered_archive_is_not_a_permanent_gap(self):
        self.add(OPENAI)
        # Newest first with a same-day reorder, then an archive far older than the window.
        items = [(article("new-b"), 5, "B"), (article("new-a"), 3, "A, reordered within the day")] + \
                [(article(f"old{n}"), 24 * (30 + n), f"Old {n}", "y" * 300) for n in range(60)]
        cut = len(rss(*items[:30])) + 10
        with patch.object(plugin, "MAX_FEED_BYTES", cut):
            source = self.source(self.fetch({OPENAI: feed_response(rss(*items))})[0])
            self.assertTrue(source["feed_truncated"])
            self.assertEqual(source["coverage"], "feed_window")
            self.assertFalse(source["possible_gap"])
            self.assertEqual([p["id"] for p in source["posts"]],
                             [feed_id(article("new-b")), feed_id(article("new-a"))])
            # A newer item placed among old ones means the unread tail may hide more.
            disordered = items[:20] + [(article("late"), 1, "Newest, out of order")] + items[20:]
            source = self.source(self.fetch({OPENAI: feed_response(rss(*disordered))})[0])
            self.assertTrue(source["feed_truncated"])
            self.assertTrue(source["possible_gap"])
            self.assertIn(feed_id(article("late")), [p["id"] for p in source["posts"]])
            # A prefix that stops inside the window still discloses a gap.
            short = len(rss(*items[:2])) + 10
            with patch.object(plugin, "MAX_FEED_BYTES", short):
                self.assertTrue(self.source(self.fetch({OPENAI: feed_response(rss(*items))})[0])["possible_gap"])

    def test_truncated_prefix_must_reach_a_day_past_the_window(self):
        self.add(OPENAI)
        # Ordered, but the readable part stops 6 h before the window start: a
        # same-day reorder could still put an in-window item in the unread tail.
        items = [(article("new"), 5, "New"), (article("edge"), 24 * 7 + 6, "Edge")] + \
                [(article(f"old{n}"), 24 * (30 + n), f"Old {n}", "y" * 300) for n in range(20)]
        with patch.object(plugin, "MAX_FEED_BYTES", len(rss(*items[:2])) + 10):
            source = self.source(self.fetch({OPENAI: feed_response(rss(*items))})[0])
        self.assertTrue(source["feed_truncated"])
        self.assertTrue(source["possible_gap"])

    def test_slow_feed_keeps_its_complete_prefix_and_discloses_the_cut(self):
        self.add(OPENAI)
        feed = rss(*[(article(f"s{n}"), n, f"Slow {n}", "z" * 400) for n in range(1, 30)])
        clock = itertools.count()
        with patch.object(plugin, "READ_DEADLINE", 3), \
                patch.object(plugin.time, "monotonic", side_effect=lambda: next(clock)):
            source = self.source(self.fetch({OPENAI: feed_response(feed, chunk=2048)}, limit=20)[0])
        self.assertEqual(source["coverage"], "feed_window")
        self.assertTrue(source["feed_truncated"])
        self.assertTrue(source["possible_gap"])
        self.assertEqual(source["lost_unattempted_now"], 0)
        self.assertTrue(0 < len(source["posts"]) < 29)

    def test_unrequested_gzip_body_is_inflated_within_the_bound(self):
        # DeepMind's CDN sometimes gzips although no Accept-Encoding was sent.
        self.add(DEEPMIND)
        post = "https://deepmind.google/blog/compressed/"
        body = gzip.compress(rss((post, 2, "Compressed item")))
        source = self.source(self.fetch({DEEPMIND: feed_response(body, url=DEEPMIND, encoding="gzip", chunk=97)})[0],
                             DEEPMIND)
        self.assertEqual((source["coverage"], [p["id"] for p in source["posts"]]),
                         ("feed_window", [feed_id(post, DEEPMIND)]))
        cases = {
            "corrupt gzip body": body[:10] + b"\x00" * 40 + body[50:],
            "truncated gzip body": body[: len(body) // 2],
        }
        for message, content in cases.items():
            with self.subTest(message):
                broken = self.source(self.fetch({DEEPMIND: feed_response(content, url=DEEPMIND, encoding="gzip")})[0],
                                     DEEPMIND)
                self.assertEqual((broken["coverage"], broken["error"]), ("unavailable", f"feed unavailable: {message}"))
        unsupported = self.source(self.fetch({DEEPMIND: feed_response(body, url=DEEPMIND, encoding="br")})[0], DEEPMIND)
        self.assertEqual(unsupported["error"], "feed unavailable: unsupported content encoding 'br'")

    def test_release_atom_is_an_owner_chosen_recordable_source(self):
        self.assertEqual(self.add(SGLANG)["channels"], [key(SGLANG)])
        notes = "<h1>Highlights</h1><p>Faster <b>decoding</b> &amp; new kernels</p><script>ignore me</script>"
        response = atom_response(atom(("v0.5.21", 2, "v0.5.21", notes), ("v0.5.20", 24 * 14, "v0.5.20")))
        result, urls = self.fetch({SGLANG: response})
        self.assertEqual(urls, [SGLANG])
        source = self.source(result, SGLANG)
        self.assertEqual((source["kind"], source["format"], source["coverage"], source["possible_gap"]),
                         ("rss", "atom", "feed_window", False))
        [item] = source["posts"]  # the 14-day-old release predates the new subscription's window
        self.assertEqual(item["id"], release_id("v0.5.21"))
        self.assertEqual(item["url"], "https://github.com/sgl-project/sglang/releases/tag/v0.5.21")
        self.assertEqual((item["title"], item["excerpt"]), ("v0.5.21", "Highlights Faster decoding & new kernels"))
        self.assertEqual(item["date"], (NOW - timedelta(hours=2)).isoformat())
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=[item["id"]])["ok"])
        self.assertEqual(self.source(self.fetch({SGLANG: response})[0], SGLANG)["posts"], [])
        # The complete feed is authoritative: an unattempted release that left it is lost.
        self.fetch({SGLANG: atom_response(atom(("v0.5.23", 1, "v0.5.23"), ("v0.5.22", 1.5, "v0.5.22"),
                                               ("v0.5.21", 2, "v0.5.21")))})
        later = self.source(self.fetch({SGLANG: atom_response(atom(("v0.5.23", 1, "v0.5.23"),
                                                                   ("v0.5.21", 2, "v0.5.21")))})[0], SGLANG)
        self.assertEqual((later["lost_unattempted_now"], later["possible_gap"]), (1, True))
        self.assertEqual([p["id"] for p in later["posts"]], [release_id("v0.5.23")])

    def test_atom_identity_dates_and_text_constructs(self):
        self.add(SGLANG)
        body = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                '<entry><id>urn:release:a</id><published>2026-09-27T10:00:00+02:00</published>'
                '<updated>2026-09-27T11:00:00Z</updated><link rel="enclosure" href="https://example.org/a.tgz"/>'
                '<link href="https://example.org/a"/><title type="html">&lt;b&gt;A&lt;/b&gt; release</title>'
                '<summary>Short summary</summary><content type="html">&lt;p&gt;Long notes&lt;/p&gt;</content></entry>'
                '<entry><updated>2026-09-27T09:00:00Z</updated><link rel="alternate" href="https://example.org/b"/>'
                '<title>B</title><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Nested '
                '<b>xhtml</b> notes</p></div></content></entry>'
                '<entry><updated>2026-09-27T08:00:00Z</updated><title>No identity</title></entry>'
                '<entry><id>urn:release:undated</id><updated>yesterday</updated><title>Undated</title></entry>'
                '</feed>').encode()
        source = self.source(self.fetch({SGLANG: atom_response(body)})[0], SGLANG)
        self.assertEqual(source["coverage"], "feed_window")
        self.assertEqual(source["skipped_items"], {"duplicate": 0, "undated": 1, "unidentified": 1})
        first, second = source["posts"]
        self.assertEqual((first["id"], first["url"], first["title"], first["excerpt"], first["date"]),
                         (feed_id("urn:release:a", SGLANG), "https://example.org/a", "A release",
                          "Short summary", "2026-09-27T08:00:00+00:00"))
        self.assertEqual((second["id"], second["url"], second["excerpt"], second["date"]),
                         (feed_id("https://example.org/b", SGLANG), "https://example.org/b",
                          "Nested xhtml notes", "2026-09-27T09:00:00+00:00"))

    def test_generic_feed_bound_reads_long_release_notes_whole(self):
        # Real release feeds are ~1.3 MiB (ten full notes); one generic bound now covers them.
        notes = "<p>" + "kernel tuning " * 15000 + "</p>"
        feed = atom(*[(f"v0.5.{n}", n, f"v0.5.{n}", notes) for n in range(1, 6)])
        self.assertGreater(len(feed), 256 * 1024)
        with patch.object(plugin._OPENER, "open", side_effect=route({SGLANG: atom_response(feed)})):
            items, facts, error = plugin._fetch_feed(SGLANG)
        self.assertEqual((error, facts["format"], facts["feed_truncated"], len(items)), ("", "atom", False, 5))
        self.assertTrue(all(item["excerpt_truncated"] and len(item["excerpt"]) <= plugin.MAX_POST_TEXT for item in items))
        with patch.object(plugin, "MAX_FEED_BYTES", 256 * 1024), \
                patch.object(plugin._OPENER, "open", side_effect=route({SGLANG: atom_response(feed)})):
            _items, capped, _error = plugin._fetch_feed(SGLANG)
        self.assertTrue(capped["feed_truncated"])

    def test_deeply_nested_feed_parses_in_linear_time(self):
        # Well-formed, below MAX_FEED_BYTES, with elements outside any item nested
        # about 300,000 deep: comparing the whole path per element took minutes.
        item = (b"<item><guid>urn:deep</guid><link>https://example.org/deep</link><title>Deep</title>"
                b"<x><title>Nested, not the item's own</title></x>"
                b"<pubDate>Sun, 27 Sep 2026 10:00:00 GMT</pubDate></item>")
        head, tail = b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>', item + b"</channel></rss>"
        copied = []

        class CountedPath(list):
            def __getitem__(self, index):
                part = super().__getitem__(index)
                if isinstance(index, slice):
                    copied.append(len(part))
                return part

            def __iter__(self):
                copied.append(len(self))
                return super().__iter__()

        depth = 2000
        feed = plugin._Feed()
        feed.path = CountedPath()
        feed.parser.Parse(head + b"<a>" * depth + b"</a>" * depth + tail, True)
        self.assertEqual([entry["title"] for entry in feed.items], ["Deep"])
        # An element looks at most at the few path entries an item spans, never at the whole path.
        self.assertLessEqual(sum(copied), 2 * (depth + 10))
        depth = (plugin.MAX_FEED_BYTES - len(head) - len(tail)) // len(b"<a></a>")
        document = head + b"<a>" * depth + b"</a>" * depth + tail
        self.assertGreater(len(document), plugin.MAX_FEED_BYTES - 16)
        with patch.object(plugin._OPENER, "open", side_effect=route({OPENAI: feed_response(document)})):
            started = time.monotonic()
            items, facts, error = plugin._fetch_feed(OPENAI)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15)  # about 0.3 s here; the quadratic parser needed minutes
        self.assertEqual((error, facts["feed_truncated"]), ("", False))
        self.assertEqual([(entry["url"], entry["title"], entry["date"]) for entry in items],
                         [("https://example.org/deep", "Deep", "2026-09-27T10:00:00+00:00")])

    def test_unattempted_items_gone_from_the_feed_are_forgotten_after_the_window(self):
        # A busy feed nobody records: each day lists only that day's 30 items, and one pinned item.
        self.add(OPENAI)
        pinned = (article("pinned"), 2, "Still listed")
        returned = {}
        for day in range(30):
            self.utcnow.return_value = NOW + timedelta(days=day)
            items = [(article(f"d{day}-{n}"), 1 - 24 * day, f"Day {day} item {n}") for n in range(30)]
            source = self.source(self.fetch({OPENAI: feed_response(rss(pinned, *items))}, limit=2)[0])
            returned[day] = [p["id"] for p in source["posts"]]
        with plugin._connect() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0]
        # The 31 listed items, plus those that vanished within the last LOOKBACK_DAYS + 1 reads;
        # without pruning, every vanished item of the 30 days (901 rows) stays.
        self.assertEqual(rows, 31 + 30 * (plugin.LOOKBACK_DAYS + 1))
        self.assertEqual((source["historical_lost_unattempted"], source["lost_unattempted_now"]), (29 * 30, 30))
        self.assertEqual(returned[29][0], feed_id(article("pinned")))  # still listed, so kept and offered
        recent, old = returned[25][1], returned[5][1]
        # Gone for 4 days, an item that was returned can still be recorded; gone for 24, it is forgotten.
        self.assertTrue(self.call("record_attempt", day="2026-10-26", post_ids=[recent])["ok"])
        self.assertFalse(self.call("record_attempt", day="2026-10-26", edition="old", post_ids=[old])["ok"])
        # Pruning never touches attempted identities: the recorded one stays after it would have expired.
        self.utcnow.return_value = NOW + timedelta(days=40)
        self.fetch({OPENAI: feed_response(rss(pinned))})
        with plugin._connect() as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT attempted FROM feed_items WHERE key=?",
                                                                 (recent.rsplit("/", 1)[1],))], [(1,)])

    def test_item_missing_before_lost_at_existed_keeps_a_full_window_from_the_upgrade(self):
        path = Path(self.api.root) / "digest.sqlite3"
        path.unlink(missing_ok=True)
        gone = feed_id(article("gone")).rsplit("/", 1)[1]
        with sqlite3.connect(path) as db:  # feed_items as the previous revision wrote it, one item already missing
            db.execute("CREATE TABLE feed_items (source TEXT NOT NULL, key TEXT NOT NULL, published TEXT NOT NULL, "
                       "attempted INTEGER NOT NULL DEFAULT 0, returned INTEGER NOT NULL DEFAULT 0, "
                       "lost_reported INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source,key))")
            db.execute("INSERT INTO feed_items VALUES (?,?,?,0,1,1)", (key(OPENAI), gone, stamp(30)))
        upgraded = datetime.now(timezone.utc)  # the backfill stamps SQLite's own clock
        self.add(OPENAI)
        feed = {OPENAI: feed_response(rss((article("listed"), 1, "Listed")))}
        for days, kept in ((plugin.LOOKBACK_DAYS - 1, True), (plugin.LOOKBACK_DAYS + 1, False)):
            with self.subTest(days=days):
                self.utcnow.return_value = upgraded + timedelta(days=days)
                self.fetch(feed)
                with plugin._connect() as conn:
                    stamps = [row[0] for row in conn.execute("SELECT lost_at FROM feed_items WHERE key=?", (gone,))]
                self.assertEqual(len(stamps), int(kept))
                if kept:
                    self.assertLess(abs(datetime.fromisoformat(stamps[0]) - upgraded), timedelta(minutes=1))

    def test_possible_gap_is_judged_by_the_window_until_a_record_then_by_the_attempted_position(self):
        self.add(OPENAI)
        recent = feed_response(rss((article("b"), 20, "B"), (article("a"), 30, "A")))
        before, _ = self.fetch({OPENAI: recent})
        # Nothing older than 30 h is listed, so earlier items of the 7-day window may be missing.
        self.assertEqual((self.source(before)["possible_gap"], self.source(before)["attempted_through"]), (True, ""))
        self.assertTrue(self.call("record_attempt", day="2026-09-27", fetch_ids=[before["fetch_id"]])["ok"])
        # The same feed after the record: it reaches back to the newest attempted item.
        after = self.source(self.fetch({OPENAI: recent})[0])
        self.assertEqual((after["possible_gap"], after["attempted_through"], after["posts"]), (False, stamp(20), []))
        # A feed that no longer reaches back to the attempted position is a gap again, though nothing unattempted was lost.
        moved = self.source(self.fetch({OPENAI: feed_response(rss((article("c"), 1, "C")))})[0])
        self.assertEqual((moved["possible_gap"], moved["lost_unattempted_now"], [p["id"] for p in moved["posts"]]),
                         (True, 0, [feed_id(article("c"))]))

    def feed_rows(self):
        with plugin._connect() as conn:
            return (conn.execute("SELECT key,attempted,returned,lost_reported,lost_at FROM feed_items "
                                 "ORDER BY key").fetchall(),
                    conn.execute("SELECT * FROM gaps").fetchall())

    def test_namespace_uri_past_its_cap_is_refused_before_any_name_carries_it(self):
        # The reviewers' shape: one long URI, then many small names in it. Each name used
        # to carry the whole URI: a 1 MiB feed held 134 million characters of names.
        self.add(OPENAI)
        self.fetch({OPENAI: feed_response(rss((article("a"), 2, "A")))})
        before = self.feed_rows()

        def feed(uri):
            names = b'<x xmlns:q="%s">%s</x>' % (uri, b"".join(b"<q:n%d/>" % n for n in range(128)))
            return rss((article("b"), 1, "B")).replace(b"<channel>", b"<channel>" + names, 1)

        for size in (plugin.MAX_NAMESPACE_URI + 1, 8 * 1024):
            with self.subTest(uri_characters=size):
                body = feed(b"u" * size)
                parsed = plugin._Feed()
                started = time.monotonic()
                with self.assertRaises(plugin._FeedRefused) as refused:
                    parsed.parse(body, True, plugin._Deadline())
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(str(refused.exception),
                                 f"feed declares an XML namespace longer than {plugin.MAX_NAMESPACE_URI} characters; refused")
                # The parser met the URI once, as its declaration; no name was built from it.
                self.assertLess(sum(map(len, parsed.parser.intern)), size + 200)
                source = self.source(self.fetch({OPENAI: feed_response(body)})[0])
                self.assertEqual((source["coverage"], source["posts"], source["lost_unattempted_now"]),
                                 ("unavailable", [], 0))
                self.assertEqual(self.feed_rows(), before)  # a is not lost, b not tracked
        at_cap = b"https://example.org/" + b"u" * (plugin.MAX_NAMESPACE_URI - 20)
        readable = self.source(self.fetch({OPENAI: feed_response(feed(at_cap))})[0])
        self.assertEqual((readable["coverage"], [p["title"] for p in readable["posts"]]), ("feed_window", ["B"]))

    def test_more_distinct_xml_names_than_the_cap_are_refused(self):
        self.add(OPENAI)
        self.fetch({OPENAI: feed_response(rss((article("a"), 2, "A")))})
        before = self.feed_rows()
        names = [b"n%d" % n for n in range(plugin.MAX_XML_NAMES + 1)]

        def feed(extra):
            return rss((article("b"), 1, "B")).replace(b"<channel>", b"<channel>" + extra, 1)

        cases = {
            "element names": b"".join(b"<%s/>" % name for name in names),
            # Each tag well within one parse piece, so only the names bound applies.
            "attribute names": b"".join(b"<x %s/>" % b" ".join(b'%s=""' % name for name in names[n:n + 500])
                                        for n in range(0, len(names), 500)),
            "namespace prefixes": b"".join(b'<x xmlns:%s="urn:x"/>' % name for name in names),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                parsed = plugin._Feed()
                with self.assertRaises(plugin._FeedRefused) as refused:
                    parsed.parse(feed(extra), True, plugin._Deadline())
                self.assertEqual(str(refused.exception), f"feed uses more than {plugin.MAX_XML_NAMES} distinct XML names; refused")
                # Checked at every tag: the parser holds at most one tag's names past the cap.
                self.assertLessEqual(len(parsed.parser.intern), plugin.MAX_XML_NAMES + 520)
                source = self.source(self.fetch({OPENAI: feed_response(feed(extra))})[0])
                self.assertEqual((source["coverage"], source["posts"]), ("unavailable", []))
                self.assertEqual(self.feed_rows(), before)
        under = b"".join(b"<%s/>" % name for name in names[:plugin.MAX_XML_NAMES // 2])
        self.assertEqual(self.source(self.fetch({OPENAI: feed_response(feed(under))})[0])["coverage"], "feed_window")

    def test_a_tag_or_comment_longer_than_a_parse_piece_is_refused(self):
        # expat reports every name of a tag at once, and expands each prefixed attribute of
        # a tag that declares a long URI with that URI before any check can run.
        self.add(OPENAI)

        def feed(extra):
            return rss((article("b"), 1, "B")).replace(b"<channel>", b"<channel>" + extra, 1)

        long = 2 * plugin.XML_PIECE
        cases = {
            "comment": b"<!--" + b"c" * long + b"-->",
            "one tag of many attributes": b"<x " + b" ".join(b'a%d=""' % n for n in range(long // 7)) + b"/>",
            "one tag declaring a long URI for its own attributes":
                b'<x xmlns:p="' + b"u" * (long // 2) + b'" ' + b" ".join(b'p:a%d=""' % n for n in range(long // 20)) + b"/>",
        }
        for label, extra in cases.items():
            with self.subTest(label):
                parsed = plugin._Feed()
                with self.assertRaises(plugin._FeedRefused) as refused:
                    parsed.parse(feed(extra), True, plugin._Deadline())
                self.assertEqual(str(refused.exception),
                                 f"feed has a tag or comment longer than {plugin.XML_PIECE // 1024} KiB; refused")
                self.assertLess(len(parsed.parser.intern), 20)  # the tag never reached the handlers
                self.assertEqual(self.source(self.fetch({OPENAI: feed_response(feed(extra))})[0])["coverage"],
                                 "unavailable")
        shorter = b"<!--" + b"c" * (plugin.XML_PIECE // 2) + b"-->"
        self.assertEqual(self.source(self.fetch({OPENAI: feed_response(feed(shorter))})[0])["coverage"], "feed_window")
        def straddling(tag_bytes):
            """An item whose enclosure tag starts at a piece boundary and ends in a shorter last piece."""
            head = b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
            item = (b"<item><guid>urn:straddle</guid><title>Straddle</title><pubDate>%s</pubDate>"
                    % email.utils.format_datetime(NOW - timedelta(hours=1), usegmt=True).encode())
            filler = b"p" * (plugin.XML_PIECE - len(head) - len(item) - len(b"<description></description>"))
            tag = b'<enclosure url="https://example.org/' + b"e" * (tag_bytes - 39) + b'"/>'
            body = head + b"<description>" + filler + b"</description>" + item + tag + b"</item></channel></rss>"
            self.assertEqual((body.index(b"<enclosure"), len(tag)), (plugin.XML_PIECE, tag_bytes))
            return body

        # Pending one whole piece, then finished: within the bound, so read. expat 2.6+ would
        # defer such a last piece and keep reporting the tag as pending; the skill turns that off.
        for size in (plugin.XML_PIECE + 100, 2 * plugin.XML_PIECE - 260):
            with self.subTest(tag_bytes=size):
                parsed = plugin._Feed()
                parsed.parse(straddling(size), True, plugin._Deadline())
                self.assertEqual([entry["title"] for entry in parsed.items], ["Straddle"])
        source = self.source(self.fetch({OPENAI: feed_response(straddling(plugin.XML_PIECE + 100))})[0])
        self.assertEqual((source["coverage"], [p["title"] for p in source["posts"]]), ("feed_window", ["Straddle"]))
        # Where deferral stays on, expat reports no position (-1) for a held-back piece; that is
        # not taken for an oversized tag.
        parsed = plugin._Feed()
        if hasattr(parsed.parser, "SetReparseDeferralEnabled"):
            parsed.parser.SetReparseDeferralEnabled(True)
            parsed.parse(straddling(2 * plugin.XML_PIECE - 260), True, plugin._Deadline())
            self.assertEqual([entry["title"] for entry in parsed.items], ["Straddle"])

    def test_parsing_that_outlasts_the_source_deadline_is_unavailable_and_records_nothing(self):
        self.add(OPENAI)
        self.fetch({OPENAI: feed_response(rss((article("a"), 2, "A"), (article("b"), 1, "B")))})
        before = self.feed_rows()
        # The next read lists b without a, behind markup spanning several parse pieces. Every
        # element costs a quarter second of this clock: parsing it all would take a minute.
        slow = rss(*[(article(f"s{n}"), 3, f"S{n}", "z" * 2000) for n in range(40)], (article("b"), 1, "B"))
        self.assertGreater(len(slow), 4 * plugin.XML_PIECE)
        clock = [1000.0]
        start = plugin._Feed._start

        def costly(feed, tag, attrs):
            clock[0] += 0.25
            start(feed, tag, attrs)

        with patch.object(plugin.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(plugin._Feed, "_start", costly):
            source = self.source(self.fetch({OPENAI: feed_response(slow)})[0])
        self.assertEqual((source["coverage"], source["error"]),
                         ("unavailable", "feed unavailable: parsing did not finish within the source deadline"))
        self.assertEqual((source["posts"], source["lost_unattempted_now"]), ([], 0))
        self.assertEqual(self.feed_rows(), before)  # a is not lost, the s items are not tracked
        # Stopped about one piece past the deadline (about 41 s here), not run to the end (62 s).
        whole = 0.25 * (slow.count(b"<") - slow.count(b"</") - slow.count(b"<![CDATA[") - slow.count(b"<?"))
        self.assertGreater(whole, plugin.READ_DEADLINE + 25)
        self.assertLess(clock[0] - 1000.0, plugin.READ_DEADLINE + 15)

    def test_unlisted_items_of_an_always_cut_feed_are_capped_and_counted(self):
        # An archive feed cut at its bound on every read: each day the readable part holds only
        # that day's ten items, and what leaves it is never proven gone.
        self.add(OPENAI)
        archive = []
        returned = {}
        with patch.object(plugin, "MAX_UNLISTED_FEED_ITEMS", 25), patch.object(plugin, "MAX_FEED_ITEMS", 10):
            for day in range(8):
                self.utcnow.return_value = NOW + timedelta(days=day)
                today = [(article(f"d{day}-{n}"), 1 - 24 * day + n / 10, f"Day {day} item {n}") for n in range(10)]
                archive = today + archive
                source = self.source(self.fetch({OPENAI: feed_response(rss(*archive))})[0])
                returned[day] = [p["id"] for p in source["posts"]]
                if day == 2:
                    self.assertTrue(self.call("record_attempt", day="2026-09-29", post_ids=returned[2][:1])["ok"])
            with plugin._connect() as conn:
                unattempted = [row[0] for row in conn.execute("SELECT key FROM feed_items WHERE attempted=0")]
        self.assertTrue(source["feed_truncated"])
        # Ten listed plus the 25 newest unlisted; 80 - 1 recorded - 10 - 25 = 44 dropped, the last ten now.
        self.assertEqual(len(unattempted), 10 + 25)
        self.assertEqual((source["dropped_unattempted_now"], source["historical_dropped_unattempted"],
                          source["possible_gap"], source["lost_unattempted_now"]), (10, 44, True, 0))
        # A failed read lists nothing, which proves nothing: it drops nothing.
        with patch.object(plugin, "MAX_UNLISTED_FEED_ITEMS", 25):
            failed = self.source(self.fetch({OPENAI: plugin.urllib.error.HTTPError(OPENAI, 503, "Busy", {}, None)})[0])
        self.assertEqual((failed["coverage"], failed.get("historical_dropped_unattempted")), ("unavailable", 44))
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feed_items WHERE attempted=0").fetchone()[0], 10 + 25)
        # Oldest first: day 6's returned items are still recordable, day 0's are gone, the recorded one stays.
        self.assertTrue(self.call("record_attempt", day="2026-10-04", post_ids=returned[6][:2])["ok"])
        self.assertFalse(self.call("record_attempt", day="2026-10-04", edition="old", post_ids=returned[0][:1])["ok"])
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT attempted FROM feed_items WHERE key=?",
                                          (returned[2][0].rsplit("/", 1)[1],)).fetchone()[0], 1)

    def test_items_already_counted_lost_are_not_counted_again_when_the_cap_drops_them(self):
        self.add(OPENAI)
        with patch.object(plugin, "MAX_UNLISTED_FEED_ITEMS", 25):
            for day in range(5):  # complete reads: each day's ten items replace the last
                self.utcnow.return_value = NOW + timedelta(days=day)
                today = [(article(f"d{day}-{n}"), 1 - 24 * day + n / 10, f"Day {day} item {n}") for n in range(10)]
                source = self.source(self.fetch({OPENAI: feed_response(rss(*today))})[0])
        self.assertFalse(source["feed_truncated"])
        self.assertEqual((source["historical_lost_unattempted"], source["lost_unattempted_now"]), (40, 10))
        self.assertNotIn("historical_dropped_unattempted", source)
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM feed_items WHERE attempted=0").fetchone()[0], 10 + 25)

    def lose_item_a(self):
        """a is returned on day 0 and missing from a complete read on day 1."""
        self.add(OPENAI)
        self.fetch({OPENAI: feed_response(rss((article("a"), 2, "A")))})
        self.utcnow.return_value = NOW + timedelta(days=1)
        lost = self.source(self.fetch({OPENAI: feed_response(rss((article("b"), 1 - 24, "B")))})[0])
        self.assertEqual(lost["lost_unattempted_now"], 1)
        self.utcnow.return_value = NOW + timedelta(days=plugin.LOOKBACK_DAYS + 2)  # past a's prune window

    def test_item_back_in_the_feed_keeps_the_row_the_agent_saw(self):
        self.lose_item_a()
        # a is listed again, behind an older item that takes the single returned slot. The read
        # ends a's absence before pruning, so a stays the returned, recordable row it was.
        back = rss((article("a"), 2, "A"), (article("older"), 3, "Older"), (article("b"), 1 - 24, "B"))
        source = self.source(self.fetch({OPENAI: feed_response(back)}, limit=1)[0])
        self.assertEqual([p["id"] for p in source["posts"]], [feed_id(article("older"))])
        self.assertTrue(self.call("record_attempt", day="2026-10-06", post_ids=[feed_id(article("a"))])["ok"])

    def test_failed_read_prunes_nothing(self):
        self.lose_item_a()
        failed = self.source(self.fetch({OPENAI: plugin.urllib.error.HTTPError(OPENAI, 503, "Busy", {}, None)})[0])
        self.assertEqual(failed["coverage"], "unavailable")
        # Only a current successful read forgets a lost item; until then it stays recordable.
        self.assertTrue(self.call("record_attempt", day="2026-10-06", post_ids=[feed_id(article("a"))])["ok"])

    def test_stale_read_neither_resets_a_loss_nor_prunes(self):
        self.add(OPENAI)
        both = rss((article("a"), 2, "A"), (article("b"), 1, "B"))
        self.fetch({OPENAI: feed_response(both)})
        older, newer = plugin._Feed(), plugin._Feed()
        older.parser.Parse(both, True)
        newer.parser.Parse(rss((article("b"), 1, "B")), True)
        calls = 0

        def overlapping(_url):
            nonlocal calls
            calls += 1
            if calls == 1:
                # A newer read completes while this one waits and finds a gone.
                found = self.source(self.call("fetch_posts"))
                self.assertEqual((found["lost_unattempted_now"], found["historical_lost_unattempted"]), (1, 1))
                return older.items, {"feed_truncated": False}, ""  # this older snapshot still lists a
            return newer.items, {"feed_truncated": False}, ""

        with patch.object(plugin, "_fetch_feed", side_effect=overlapping):
            self.assertTrue(self.source(self.call("fetch_posts"))["stale_read"])
            later = self.source(self.call("fetch_posts"))
        # a is still gone and was counted once: the stale snapshot did not reset its loss.
        self.assertEqual((later["lost_unattempted_now"], later["historical_lost_unattempted"]), (0, 1))

    def test_ipv6_forms_carrying_a_non_public_ipv4_address_are_refused(self):
        refused = {
            "IPv4-mapped": ["::ffff:10.0.0.1", "::ffff:127.0.0.1"],
            "IPv4-compatible": ["::127.0.0.1", "::169.254.169.254", "::a00:1"],
            "IPv4-translated": ["::ffff:0:a00:1", "::ffff:0:7f00:1"],
            "6to4": ["2002:a00:1::", "2002:7f00:1::1"],
            "Teredo, client 10.0.0.1": ["2001:0:4136:e378:8000:63bf:f5ff:fffe"],
            "NAT64": ["64:ff9b::a00:1", "64:ff9b::7f00:1"],
            "local-use NAT64": ["64:ff9b:1::a00:1", "64:ff9b:1::7f00:1"],
        }
        for form, addresses in refused.items():
            for address in addresses:
                with self.subTest(form=form, address=address):
                    self.assertFalse(plugin._is_public(address))
        # Only the embedded address decides: the same forms around a public one stay usable.
        for address in ("::ffff:8.8.8.8", "::808:808", "::ffff:0:808:808", "64:ff9b::808:808"):
            with self.subTest(public=address):
                self.assertTrue(plugin._is_public(address))
        self.add(OPENAI)
        for address in ("::127.0.0.1", "::ffff:0:a00:1"):
            with self.subTest(resolved=address):
                self.resolve.return_value = [address]
                result, urls = self.fetch({})
                self.assertEqual(urls, [])
                self.assertEqual(self.source(result)["error"],
                                 f"feed unavailable: refused: openai.com resolves to non-public address {address}")


def telegram_pages(pages):
    """An opener side effect serving each channel's own preview page."""
    def opened(request, timeout):
        return FakeResponse(content=pages[request.full_url.rsplit("/", 1)[1]], url=request.full_url)
    return opened


def join_left_behind():
    """Wait for reads a fetch reported and left behind, so nothing outlives a test's patches."""
    for thread in threading.enumerate():
        if thread.name.startswith("science-digest"):
            thread.join(10)


class EditionBindingTests(ConsumerCase):
    """Finding A: an edition advances a source only through a fetch it names or ids it supplies."""

    # A readable pre-window post, then a post without text: a mixed page whose new portion has no readable post.
    VERIFIED = page(post_html(1, "Old", hours_ago=24 * 30), post_html(2, extra=PHOTO))
    # The same channel later, with no extractable text anywhere on the page.
    WHOLLY_TEXTLESS = page(post_html(2, extra=PHOTO), post_html(3, extra=PLACEHOLDER))
    OTHER = page(post_html(1, "Other news", channel="other_news"))

    def setUp(self):
        super().setUp()
        for name in ("sample_news", "other_news"):
            self.call("channels", action="add", name=name)

    def fetch_pages(self, pages, **kwargs):
        with patch.object(plugin._OPENER, "open", side_effect=telegram_pages(pages)):
            return self.call("fetch_posts", **kwargs)

    def verify(self):
        source = self.fetch_pages({"sample_news": self.VERIFIED}, channel="sample_news")["channels"][0]
        self.assertEqual([p["id"] for p in source["text_not_extracted"]], ["sample_news/2"])

    def test_wholly_textless_page_does_not_advance_from_an_earlier_verification_fetch(self):
        self.verify()
        result = self.fetch_pages({"sample_news": self.WHOLLY_TEXTLESS, "other_news": self.OTHER})
        rows = {row["channel"]: row for row in result["channels"]}
        self.assertEqual(rows["sample_news"]["coverage"], "unavailable")
        self.assertEqual([p["id"] for p in rows["other_news"]["posts"]], ["other_news/1"])
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["other_news/1"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 0, "ids": []})
        self.assertEqual((self.watermark("sample_news"), self.watermark("other_news")), (0, 1))
        # The post was never skipped: it is listed again once the channel reads again.
        again = self.fetch_pages({"sample_news": self.VERIFIED}, channel="sample_news")["channels"][0]
        self.assertEqual([p["id"] for p in again["text_not_extracted"]], ["sample_news/2"])

    def test_fetch_id_of_an_edition_with_a_wholly_textless_page_advances_only_the_readable_source(self):
        self.verify()
        result = self.fetch_pages({"sample_news": self.WHOLLY_TEXTLESS, "other_news": self.OTHER})
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=[result["fetch_id"]])
        self.assertEqual(receipt["post_ids"], ["other_news/1"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 0, "ids": []})
        self.assertEqual((self.watermark("sample_news"), self.watermark("other_news")), (0, 1))

    def test_verification_of_one_source_is_not_bound_to_an_edition_of_another(self):
        self.verify()
        result = self.fetch_pages({"other_news": self.OTHER}, channel="other_news")
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=["other_news/1"])["ok"])
        self.assertEqual(self.watermark("sample_news"), 0)
        self.assertTrue(self.call("record_attempt", day="2026-09-27", edition="again",
                                  fetch_ids=[result["fetch_id"]])["ok"])
        self.assertEqual(self.watermark("sample_news"), 0)

    def test_named_post_does_not_carry_its_textless_neighbour_past_an_unavailable_page(self):
        self.fetch_pages({"sample_news": page(post_html(5, "Read"), post_html(6, extra=PHOTO))}, channel="sample_news")
        self.fetch_pages({"sample_news": self.WHOLLY_TEXTLESS}, channel="sample_news")
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/5"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 0, "ids": []})
        self.assertEqual(self.watermark("sample_news"), 5)

    def test_textless_rows_from_before_this_revision_advance_nothing(self):
        # A state file an older revision wrote holds observations, but no fetch to bind them to.
        with plugin._connect() as conn:
            conn.execute("INSERT INTO observed(channel,id,text_extracted) VALUES ('sample_news',9,0)")
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=[])
        self.assertEqual(receipt["attempted_not_read"], {"count": 0, "ids": []})
        self.assertEqual(self.watermark("sample_news"), 0)

    def test_fetch_id_records_a_mixed_portion_whose_new_part_is_only_textless(self):
        read = [post_html(30, "Readable")]
        self.fetch_pages({"sample_news": page(*read)}, channel="sample_news")
        self.assertTrue(self.call("record_attempt", day="2026-09-26", post_ids=["sample_news/30"])["ok"])
        mixed = page(*read, post_html(31, extra=PHOTO), post_html(32, extra=PLACEHOLDER))
        result = self.fetch_pages({"sample_news": mixed, "other_news": self.OTHER})
        rows = {row["channel"]: row for row in result["channels"]}
        self.assertTrue(result["ok"])
        self.assertEqual((rows["sample_news"]["coverage"], rows["sample_news"]["posts"]), ("recent_page_only", []))
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=[result["fetch_id"]])
        self.assertEqual(receipt["attempted_not_read"], {"count": 2, "ids": ["sample_news/31", "sample_news/32"]})
        self.assertEqual((self.watermark("sample_news"), self.watermark("other_news")), (32, 1))
        self.assertEqual(textless(self.fetch_pages({"sample_news": mixed}, channel="sample_news")["channels"][0]), [])

    def test_supplied_textless_id_is_recorded_from_any_returned_fetch(self):
        self.verify()
        self.fetch_pages({"other_news": self.OTHER}, channel="other_news")
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/2", "other_news/1"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 1, "ids": ["sample_news/2"]})
        self.assertEqual(self.watermark("sample_news"), 2)

    def test_textless_post_that_left_the_page_is_not_claimed_as_attempted(self):
        self.verify()
        moved_on = page(post_html(3, "Next"))
        source = self.fetch_pages({"sample_news": moved_on}, channel="sample_news")["channels"][0]
        self.assertEqual(source["lost_unattempted_now"], 1)
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/3"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 0, "ids": []})
        self.assertEqual(self.watermark("sample_news"), 3)

    def test_fetch_ids_of_a_call_and_its_deferred_follow_ups_record_the_edition_without_copying_ids(self):
        for number in range(10):
            self.call("channels", action="add", name=f"channel{number}")

        def page_of(channel):
            return ([{"id": f"{channel}/1", "url": f"https://t.me/{channel}/1", "text": "x" * 1200, "date": "",
                      "links": ["https://example.org/" + "z" * 280] * 3},
                     {"id": f"{channel}/2", "url": f"https://t.me/{channel}/2", "text": "", "date": "", "links": []}], "")

        with patch.object(plugin, "_fetch_channel", side_effect=page_of):
            result = self.call("fetch_posts", limit=20)
            latest = {row["channel"]: row for row in result["channels"]}
            deferred = [name for name, row in latest.items() if row["coverage"] == "output_deferred"]
            self.assertTrue(deferred)
            fetch_ids = [result["fetch_id"]]
            for name in deferred:
                alone = self.call("fetch_posts", channel=name)
                self.assertEqual(alone["channels"][0]["coverage"], "recent_page_only")
                latest[name] = alone["channels"][0]
                fetch_ids.append(alone["fetch_id"])
        self.assertEqual(len(set(fetch_ids)), len(fetch_ids))
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=fetch_ids)
        returned = {name: [p["id"] for p in row["posts"] + textless(row)] for name, row in latest.items()}
        self.assertEqual(receipt["post_ids"], sorted(x for ids in returned.values() for x in ids))
        self.assertEqual(receipt["attempted_not_read"]["count"], sum(len(textless(row)) for row in latest.values()))
        for name in deferred:
            self.assertEqual(self.watermark(name), 2)  # the follow-up call's whole portion, textless post included
        for name, ids in returned.items():
            self.assertEqual(self.watermark(name), max((plugin._number(x) for x in ids), default=0))

    def test_fetch_id_records_only_what_the_output_bound_kept(self):
        content = page(*[post_html(n, extra=PHOTO) if n % 3 == 0 else post_html(n, "y" * 900) for n in range(1, 13)])
        with patch.object(plugin, "MAX_OUTPUT_CHARS", 4500), \
                patch.object(plugin._OPENER, "open", side_effect=telegram_pages({"sample_news": content})):
            result = self.call("fetch_posts", channel="sample_news", limit=20)
        source = result["channels"][0]
        self.assertTrue(source["first_omitted_id"])
        boundary = plugin._number(source["first_omitted_id"])
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=[result["fetch_id"]])
        self.assertEqual(sorted(receipt["post_ids"]), sorted(p["id"] for p in source["posts"] + textless(source)))
        self.assertLess(self.watermark("sample_news"), boundary)
        following = self.fetch_pages({"sample_news": content}, channel="sample_news", limit=20)["channels"][0]
        self.assertEqual(min(plugin._number(p["id"]) for p in following["posts"] + textless(following)), boundary)

    def test_fetch_ids_and_post_ids_combine(self):
        self.verify()
        result = self.fetch_pages({"other_news": self.OTHER}, channel="other_news")
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/2"], fetch_ids=[result["fetch_id"]])
        self.assertEqual(receipt["post_ids"], ["other_news/1", "sample_news/2"])
        self.assertEqual((self.watermark("sample_news"), self.watermark("other_news")), (2, 1))

    def test_fetch_id_counts_only_while_it_is_the_sources_latest_fetch(self):
        first = self.fetch_pages({"sample_news": page(post_html(5, "Read")), "other_news": self.OTHER})
        refetch = self.fetch_pages({"sample_news": page(post_html(5, "Read"), post_html(6, "New"))}, channel="sample_news")
        # other_news still has the first call as its latest; sample_news has moved on.
        partial = self.call("record_attempt", day="2026-09-27", fetch_ids=[first["fetch_id"]])
        self.assertEqual(partial["post_ids"], ["other_news/1"])
        self.assertEqual(self.watermark("sample_news"), 0)
        latest = self.call("record_attempt", day="2026-09-28", fetch_ids=[refetch["fetch_id"]])
        self.assertEqual(latest["post_ids"], ["sample_news/5", "sample_news/6"])
        self.assertEqual(self.watermark("sample_news"), 6)

    def test_superseded_fetch_id_is_refused_without_spending_the_edition_and_replays_stay_idempotent(self):
        first = self.fetch_pages({"sample_news": page(post_html(5, "Read"))}, channel="sample_news")
        second = self.fetch_pages({"sample_news": page(post_html(5, "Read"))}, channel="sample_news")
        refused = self.call("record_attempt", day="2026-09-27", fetch_ids=[first["fetch_id"]])
        self.assertFalse(refused["ok"])
        self.assertIn("latest", refused["error"])
        recorded = self.call("record_attempt", day="2026-09-27", fetch_ids=[second["fetch_id"]])
        self.assertTrue(recorded["new_attempt"])
        replay = self.call("record_attempt", day="2026-09-27", fetch_ids=[second["fetch_id"]])
        self.assertEqual((replay["new_attempt"], replay["same_selection"]), (False, True))
        # A replay answers with the original even when its fetch has been superseded since.
        replay = self.call("record_attempt", day="2026-09-27", fetch_ids=[first["fetch_id"]])
        self.assertEqual((replay["ok"], replay["new_attempt"], replay["post_ids"]), (True, False, ["sample_news/5"]))

    def test_argument_errors_do_not_spend_the_edition(self):
        for kwargs in ({}, {"fetch_ids": ["7"]}, {"fetch_ids": [True]}, {"fetch_ids": 7},
                       {"fetch_ids": list(range(25))}):
            with self.subTest(kwargs=str(kwargs)[:40]):
                self.assertFalse(self.call("record_attempt", day="2026-09-27", **kwargs)["ok"])
        self.assertTrue(self.call("record_attempt", day="2026-09-27", post_ids=[])["new_attempt"])

    def test_removed_source_leaves_its_fetch_behind(self):
        result = self.fetch_pages({"sample_news": page(post_html(5, "Read"))}, channel="sample_news")
        self.call("channels", action="remove", name="sample_news")
        self.call("channels", action="add", name="sample_news")
        self.assertFalse(self.call("record_attempt", day="2026-09-27", fetch_ids=[result["fetch_id"]])["ok"])

    def test_feed_items_and_textless_posts_record_together_from_one_fetch_id(self):
        self.call("channels", action="add", name=OPENAI)
        self.call("channels", action="remove", name="other_news")
        self.fetch_pages({"sample_news": page(post_html(5, "Read"))}, channel="sample_news")
        self.call("record_attempt", day="2026-09-26", post_ids=["sample_news/5"])
        pages = {"https://t.me/s/sample_news": FakeResponse(content=page(post_html(5, "Read"), post_html(6, extra=PHOTO))),
                 OPENAI: feed_response(rss((article("a"), 1, "A")))}
        with patch.object(plugin._OPENER, "open", side_effect=route(pages)):
            result = self.call("fetch_posts")
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=[result["fetch_id"]])
        self.assertEqual(sorted(receipt["post_ids"]), sorted([feed_id(article("a")), "sample_news/6"]))
        self.assertEqual(receipt["attempted_not_read"]["ids"], ["sample_news/6"])
        self.assertEqual(self.watermark("sample_news"), 6)
        with patch.object(plugin._OPENER, "open", side_effect=route(pages)):
            again = self.call("fetch_posts")
        self.assertEqual([row["posts"] for row in again["channels"]], [[], []])


class RetryTests(ConsumerCase):
    """Finding B and the consumer run: include_attempted brings back what was attempted and not read, and new posts."""

    def setUp(self):
        super().setUp()
        self.call("channels", action="add", name="sample_news")

    def test_baseline_post_is_not_returned_as_attempted(self):
        content = page(post_html(1, "Months old", hours_ago=24 * 90), post_html(2, "Fresh"))
        first = self.fetch_page(content)["channels"][0]
        self.assertEqual(([p["id"] for p in first["posts"]], first["baseline_posts"]), (["sample_news/2"], 1))
        self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/2"])
        retry = self.fetch_page(content, include_attempted=True)["channels"][0]
        self.assertEqual(([p["id"] for p in retry["posts"]], textless(retry), retry["omitted_posts"]), ([], [], 0))

    def test_retry_budget_goes_to_attempted_not_read_posts_and_new_ones(self):
        # The consumer run's shape: baseline posts, read posts, many photo posts, then a new post.
        self.call("channels", action="add", name=OPENAI)
        attempted = [post_html(n, extra=PHOTO) for n in range(6, 22)]
        old = [post_html(n, "Old " + "x" * 800, hours_ago=24 * 30) for n in range(1, 4)]
        read = [post_html(n, "Read " + "y" * 800) for n in (4, 5)]
        feed = feed_response(rss((article("read"), 2, "Read item")))
        with patch.object(plugin._OPENER, "open", side_effect=route({
                "https://t.me/s/sample_news": FakeResponse(content=page(*old, *read, *attempted)), OPENAI: feed})):
            result = self.call("fetch_posts", limit=20)
        returned = [p["id"] for row in result["channels"] for p in row["posts"] + textless(row)]
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=returned)
        self.assertEqual(receipt["attempted_not_read"]["count"], 16)
        new = post_html(22, "Brand new")
        with patch.object(plugin._OPENER, "open", side_effect=route({
                "https://t.me/s/sample_news": FakeResponse(content=page(*old, *read, *attempted, new)), OPENAI: feed})):
            retry = self.call("fetch_posts", limit=20, include_attempted=True)
        rows = {row["channel"]: row for row in retry["channels"]}
        source = rows["sample_news"]
        self.assertEqual([(p["id"], p["already_attempted"]) for p in source["posts"]], [("sample_news/22", False)])
        self.assertEqual([p["id"] for p in source["text_not_extracted"]], [f"sample_news/{n}" for n in range(6, 22)])
        self.assertTrue(all(p["already_attempted"] for p in source["text_not_extracted"]))
        self.assertEqual(source["omitted_posts"], 0)
        self.assertEqual(rows[key(OPENAI)]["posts"], [])  # the read feed item does not come back either

    def test_retried_post_with_text_leaves_the_pool_once_recorded(self):
        before = [post_html(1, "Read"), post_html(2, extra=PHOTO), post_html(3, extra=PHOTO)]
        self.fetch_page(page(*before))
        self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/1", "sample_news/2", "sample_news/3"])
        after = [post_html(1, "Read"), post_html(2, "The text is back"), post_html(3, extra=PHOTO)]
        retry = self.fetch_page(page(*after), include_attempted=True)
        source = retry["channels"][0]
        self.assertEqual([(p["id"], p["already_attempted"]) for p in source["posts"]], [("sample_news/2", True)])
        self.assertEqual([p["id"] for p in source["text_not_extracted"]], ["sample_news/3"])
        # Not recorded yet: it stays retryable, whichever fetch last saw its text.
        self.assertEqual(self.fetch_page(page(*after), include_attempted=True)["channels"][0]["posts"][0]["id"],
                         "sample_news/2")
        receipt = self.call("record_attempt", day="2026-09-28", post_ids=["sample_news/2", "sample_news/3"])
        self.assertEqual(receipt["attempted_not_read"], {"count": 1, "ids": ["sample_news/3"]})
        again = self.fetch_page(page(*after), include_attempted=True)["channels"][0]
        self.assertEqual((again["posts"], [p["id"] for p in again["text_not_extracted"]]), ([], ["sample_news/3"]))
        self.assertEqual(self.watermark(), 3)

    def test_normal_edition_stays_past_a_retried_post(self):
        self.fetch_page(page(post_html(1, "Read"), post_html(2, extra=PHOTO)))
        self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/1", "sample_news/2"])
        self.fetch_page(page(post_html(1, "Read"), post_html(2, extra=PHOTO)), include_attempted=True)
        normal = self.fetch_page(page(post_html(1, "Read"), post_html(2, extra=PHOTO)))["channels"][0]
        self.assertEqual((normal["posts"], textless(normal)), ([], []))

    def test_new_feed_item_is_returned_under_include_attempted_and_a_read_one_is_not(self):
        self.call("channels", action="remove", name="sample_news")
        self.call("channels", action="add", name=OPENAI)
        with patch.object(plugin._OPENER, "open", side_effect=route({OPENAI: feed_response(rss((article("a"), 3, "A")))})):
            self.call("fetch_posts")
        self.call("record_attempt", day="2026-09-27", post_ids=[feed_id(article("a"))])
        both = feed_response(rss((article("b"), 1, "B"), (article("a"), 3, "A")))
        with patch.object(plugin._OPENER, "open", side_effect=route({OPENAI: both})):
            retry = self.call("fetch_posts", include_attempted=True)
        self.assertEqual([p["id"] for p in retry["channels"][0]["posts"]], [feed_id(article("b"))])
        # A feed item has no attempted-not-read state, so it carries no already_attempted flag.
        self.assertNotIn("already_attempted", retry["channels"][0]["posts"][0])

    def test_state_from_before_this_revision_keeps_its_attempted_not_read_posts(self):
        path = Path(self.api.root) / "digest.sqlite3"
        path.unlink()
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE observed (channel TEXT NOT NULL, id INTEGER NOT NULL, "
                       "lost_reported INTEGER NOT NULL DEFAULT 0, text_extracted INTEGER NOT NULL DEFAULT 1, "
                       "PRIMARY KEY(channel,id))")
            db.executemany("INSERT INTO observed(channel,id,text_extracted) VALUES ('sample_news',?,?)",
                           [(4, 1), (5, 0), (7, 0)])
            db.execute("CREATE TABLE seen (channel TEXT PRIMARY KEY, last_id INTEGER NOT NULL)")
            db.execute("INSERT INTO seen VALUES ('sample_news',5)")
            db.execute("CREATE TABLE channels (name TEXT PRIMARY KEY COLLATE NOCASE)")
            db.execute("INSERT INTO channels VALUES ('sample_news')")
        content = page(post_html(3, "Never returned"), post_html(4, "Read"), post_html(5, extra=PHOTO),
                       post_html(7, extra=PHOTO), post_html(8, "New"))
        retry = self.fetch_page(content, include_attempted=True)["channels"][0]
        self.assertEqual([(p["id"], p["already_attempted"]) for p in retry["posts"]], [("sample_news/8", False)])
        self.assertEqual([(p["id"], p["already_attempted"]) for p in retry["text_not_extracted"]],
                         [("sample_news/5", True), ("sample_news/7", False)])


class FirstRunCoverageTests(ConsumerCase):
    """The consumer run: a first page that does not reach back to the window start is a possible gap."""

    def setUp(self):
        super().setUp()
        self.call("channels", action="add", name="sample_news")

    def test_first_page_newer_than_the_window_start_discloses_a_possible_gap(self):
        source = self.fetch_page(page(post_html(170, "Four days ago", hours_ago=96), post_html(171, "New")))["channels"][0]
        self.assertTrue(source["possible_gap"])
        self.assertEqual(source["window_since"], (NOW - timedelta(days=7)).isoformat())
        self.assertNotIn("baseline_posts", source)

    def test_first_page_reaching_past_the_window_start_has_no_gap(self):
        source = self.fetch_page(page(post_html(169, "Old", hours_ago=24 * 8), post_html(170, "New")))["channels"][0]
        self.assertFalse(source["possible_gap"])

    def test_channel_page_starting_at_its_first_post_has_no_past_to_miss(self):
        source = self.fetch_page(page(post_html(1, "Hello", hours_ago=5), post_html(2, "New")))["channels"][0]
        self.assertFalse(source["possible_gap"])

    def test_undated_page_makes_no_window_claim(self):
        source = self.fetch_page(page(post_html(170, "No date", dated=False)))["channels"][0]
        self.assertFalse(source["possible_gap"])

    def test_attempted_channel_is_judged_by_its_cursor_instead(self):
        content = page(post_html(170, "Four days ago", hours_ago=96), post_html(171, "New"))
        self.fetch_page(content)
        self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/170", "sample_news/171"])
        source = self.fetch_page(content)["channels"][0]
        self.assertFalse(source["possible_gap"])
        self.assertNotIn("window_since", source)


class ScriptedServer:
    """One loopback HTTP exchange: after the request, each part is sent `gap` seconds apart."""

    def __init__(self, parts, gap=0.0):
        self.parts, self.gap = parts, gap
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(15)
        self.port = self.listener.getsockname()[1]
        self.url = f"https://127.0.0.1:{self.port}/feed.xml"
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(15)
                connection.recv(65536)
                for part in self.parts:
                    connection.sendall(part)
                    if self.gap:
                        time.sleep(self.gap)
        except OSError:
            pass  # the client hung up, which is what these tests want

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.listener.close()
        self.thread.join(15)


CHUNKED_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nTransfer-Encoding: chunked\r\n\r\n"


def chunk(data):
    return b"%x\r\n%s\r\n" % (len(data), data)


class TransportDeadlineTests(ConsumerCase):
    """Finding C: real urllib and http.client over real sockets; only TLS is left out."""

    # Each script takes 6 s to send, far beyond the short deadline these tests set.
    TRICKLE = [b"x"] * 80
    GAP = 0.075
    DEADLINE = 1.0
    PROMPTLY = 4

    def setUp(self):
        super().setUp()
        # The loopback server stands in for a public host: the address check admits
        # it, and the connection goes to the address that check vetted.
        self.resolve.return_value = ["127.0.0.1"]
        is_public, connect = plugin._is_public, socket.create_connection

        def loopback_only(address, *args, **kwargs):
            if address[0] != "127.0.0.1":
                raise AssertionError(f"an offline test tried to connect to {address}")
            return connect(address, *args, **kwargs)

        for patcher in (patch.object(ssl.SSLContext, "wrap_socket", lambda _self, sock, **_kwargs: sock),
                        patch.object(plugin, "_is_public", lambda value: value == "127.0.0.1" or is_public(value)),
                        patch.object(plugin.socket, "create_connection", loopback_only),
                        patch.dict(os.environ, {"no_proxy": "*"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def get(self, server, limit=64 * 1024):
        return plugin._get(server.url, plugin._FEED_ACCEPT, limit, html=False)

    def test_slow_response_headers_hit_the_source_deadline(self):
        headers = [b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\n"] + [b"X-Slow-%d: y\r\n" % n for n in range(80)]
        with ScriptedServer(headers + [b"Content-Length: 2\r\n\r\nok"], self.GAP) as server, \
                patch.object(plugin, "READ_DEADLINE", self.DEADLINE):
            started = time.monotonic()
            with self.assertRaises(plugin._Unavailable) as cut:
                self.get(server)
        self.assertLess(time.monotonic() - started, self.PROMPTLY)
        self.assertEqual(str(cut.exception), "read deadline exceeded")

    def test_slow_status_line_hits_the_source_deadline(self):
        with ScriptedServer([b"HTTP/1.1 200 OK"] + self.TRICKLE, self.GAP) as server, \
                patch.object(plugin, "READ_DEADLINE", self.DEADLINE):
            started = time.monotonic()
            with self.assertRaises(plugin._Unavailable):
                self.get(server)
        self.assertLess(time.monotonic() - started, self.PROMPTLY)

    def test_slow_chunk_framing_hits_the_source_deadline_and_keeps_the_prefix(self):
        parts = [CHUNKED_HEAD + chunk(b"hello"), b"5;ext="] + self.TRICKLE + [b"\r\nworld\r\n0\r\n\r\n"]
        with ScriptedServer(parts, self.GAP) as server, patch.object(plugin, "READ_DEADLINE", self.DEADLINE):
            started = time.monotonic()
            body, complete = self.get(server)
        self.assertLess(time.monotonic() - started, self.PROMPTLY)
        self.assertEqual((body, complete), (b"hello", False))

    def test_slow_trailer_hits_the_source_deadline_and_says_the_body_is_not_complete(self):
        parts = [CHUNKED_HEAD + chunk(b"hello") + b"0\r\nX-Trailer: "] + self.TRICKLE
        with ScriptedServer(parts, self.GAP) as server, patch.object(plugin, "READ_DEADLINE", self.DEADLINE):
            started = time.monotonic()
            body, complete = self.get(server)
        self.assertLess(time.monotonic() - started, self.PROMPTLY)
        self.assertEqual((body, complete), (b"hello", False))

    def test_normal_responses_pass_through_the_timed_transport(self):
        cases = {
            "content length": ([b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: 10\r\n\r\nhelloworld"],
                               b"helloworld"),
            "no length": ([b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\n\r\nhello", b"world"], b"helloworld"),
            "chunked": ([CHUNKED_HEAD, chunk(b"hello"), chunk(b"world"), b"0\r\n\r\n"], b"helloworld"),
            "chunked with extension and trailer": (
                [CHUNKED_HEAD + b"a;name=value\r\nhelloworld\r\n0\r\nX-Trailer: y\r\n\r\n"], b"helloworld"),
            "gzip": ([b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Encoding: gzip\r\n"
                      b"Transfer-Encoding: chunked\r\n\r\n", chunk(gzip.compress(b"helloworld")), b"0\r\n\r\n"], b"helloworld"),
            "gzip with content length": ([b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Encoding: gzip\r\n"
                                          b"Content-Length: %d\r\n\r\n" % len(gzip.compress(b"helloworld")),
                                          gzip.compress(b"helloworld")], b"helloworld"),
        }
        for label, (parts, expected) in cases.items():
            with self.subTest(label), ScriptedServer(parts, 0.01) as server:
                self.assertEqual(self.get(server), (expected, True))

    def test_body_cut_before_its_declared_length_is_refused(self):
        # http.client's read1 returns b"" instead of raising IncompleteRead when the peer closes early.
        member = gzip.compress(b"<rss/>")
        cases = {
            "content length 500, 50 bytes, then FIN":
                b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: 500\r\n\r\n" + b"x" * 50,
            "complete gzip member, shorter than its content length":
                b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Encoding: gzip\r\n"
                b"Content-Length: %d\r\n\r\n%s" % (len(member) + 100, member),
        }
        for label, response in cases.items():
            with self.subTest(label), ScriptedServer([response]) as server:
                with self.assertRaises(plugin._Unavailable) as cut:
                    self.get(server)
                self.assertEqual(str(cut.exception), "body ended before its declared length")

    def test_preview_cut_before_its_declared_length_does_not_make_unread_posts_look_lost(self):
        self.call("channels", action="add", name="sample_news")
        full = page(*[post_html(n, f"Post {n}") for n in (1, 2, 3, 4)])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=full)):
            self.call("fetch_posts")  # four posts returned, none attempted yet
        cut = full[:full.index(b'data-post="sample_news/2"')]  # post 1 whole, then the connection closes
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n\r\n" % len(full)
        with ScriptedServer([head + cut]) as server:
            # The very response object urllib hands over, read from a real socket.
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", "/s/sample_news")
            response = connection.getresponse()
            response.url = "https://t.me/s/sample_news"
            with patch.object(plugin._OPENER, "open", return_value=response):
                source = self.call("fetch_posts")["channels"][0]
        self.assertEqual((source["coverage"], source["error"]),
                         ("unavailable", "preview unavailable: body ended before its declared length"))
        self.assertEqual((source["lost_unattempted_now"], source["historical_lost_unattempted"]), (0, 0))
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM observed WHERE lost_reported=1").fetchone()[0], 0)
        # Nothing was marked lost, so naming a later returned post cannot pass over the unread ones.
        refused = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/1", "sample_news/4"])
        self.assertIn("every earlier returned post", refused["error"])
        self.assertEqual(self.watermark(), 0)
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=full)):
            whole = self.call("fetch_posts")["channels"][0]
        self.assertEqual(([p["id"] for p in whole["posts"]], whole["lost_unattempted_now"]),
                         ([f"sample_news/{n}" for n in (1, 2, 3, 4)], 0))

    def test_preview_cut_without_any_declared_length_does_not_make_unread_posts_look_lost(self):
        self.call("channels", action="add", name="sample_news")
        full = page(*[post_html(n, f"Post {n}") for n in (1, 2, 3, 4)])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=full)):
            self.call("fetch_posts")  # four posts returned, none attempted yet
        cut = full[:full.index(b'data-post="sample_news/2"')]  # post 1 whole, then the connection closes
        # Neither Content-Length nor chunked: HTTP lets the close itself end the body.
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n"
        with ScriptedServer([head + cut]) as server:
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", "/s/sample_news")
            response = connection.getresponse()
            response.url = "https://t.me/s/sample_news"
            with patch.object(plugin._OPENER, "open", return_value=response):
                source = self.call("fetch_posts")["channels"][0]
        self.assertEqual((source["coverage"], source["error"]),
                         ("unavailable", "preview unavailable: the page ended before its closing </html> tag"))
        self.assertEqual((source["lost_unattempted_now"], source["historical_lost_unattempted"]), (0, 0))
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM observed WHERE lost_reported=1").fetchone()[0], 0)
        refused = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/1", "sample_news/4"])
        self.assertIn("every earlier returned post", refused["error"])
        # A whole page is read; Telegram follows </html> with a comment (t.me/s/durov, 2026-11-02).
        whole_page = full + b"\n<!-- page generated in 50.8ms -->\n"
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(content=whole_page)):
            whole = self.call("fetch_posts")["channels"][0]
        self.assertEqual(([p["id"] for p in whole["posts"]], whole["lost_unattempted_now"]),
                         ([f"sample_news/{n}" for n in (1, 2, 3, 4)], 0))

    def test_connection_goes_to_the_checked_address_and_tls_verifies_the_url_host(self):
        # DNS rebinding: the check saw the public stand-in; a second lookup would now name another host.
        internal = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(internal.close)
        lookup = socket.getaddrinfo

        def rebinding(host, port, *args, **kwargs):
            if host == "feed.example":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", internal.getsockname())]
            return lookup(host, port, *args, **kwargs)

        hostnames = []

        def handshake(_context, sock, server_hostname=None, **_kwargs):
            hostnames.append(server_hostname)
            return sock

        body = b"<rss/>"
        with ScriptedServer([b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: %d\r\n\r\n%s"
                             % (len(body), body)]) as server, \
                patch.object(plugin.socket, "getaddrinfo", side_effect=rebinding), \
                patch.object(ssl.SSLContext, "wrap_socket", handshake):
            url = f"https://feed.example:{server.port}/feed.xml"
            self.assertEqual(plugin._get(url, plugin._FEED_ACCEPT, 1024, html=False), (body, True))
        self.resolve.assert_called_once_with("feed.example", server.port)
        self.assertEqual(hostnames, ["feed.example"])  # SNI and certificate checks use the URL's host
        internal.setblocking(False)
        with self.assertRaises(BlockingIOError):
            internal.accept()  # the rebound address was never contacted

    def test_only_a_proxy_tunnel_is_dialled_by_name(self):
        dialled = []

        def refuse(target, _timeout, _source):
            dialled.append(target)
            raise ConnectionRefusedError("refused by the test")

        proxy = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.80", 3128))]
        for tunnel, expected in ((None, [("203.0.113.9", 443), ("203.0.113.10", 443)]), ("feed.example", [("192.0.2.80", 3128)])):
            with self.subTest(tunnel=tunnel), \
                    patch.object(plugin.socket, "getaddrinfo", return_value=proxy) as resolved, \
                    patch.object(plugin.socket, "create_connection", side_effect=refuse):
                dialled.clear()
                connection = plugin._TimedConnection("proxy.example" if tunnel else "feed.example",
                                                     3128 if tunnel else 443, timeout=plugin.REQUEST_TIMEOUT,
                                                     deadline=plugin._Deadline(), addresses=["203.0.113.9", "203.0.113.10"])
                if tunnel:
                    connection.set_tunnel(tunnel, 443)
                with self.assertRaises(ConnectionRefusedError):
                    connection.connect()
                self.assertEqual(dialled, expected)
                # Direct connections never look the name up again; a proxy is found by its own name.
                self.assertEqual(resolved.call_args_list,
                                 [call("proxy.example", 3128, type=socket.SOCK_STREAM)] if tunnel else [])

    def test_status_errors_and_redirects_still_name_themselves(self):
        with ScriptedServer([b"HTTP/1.1 503 Busy\r\nContent-Length: 0\r\n\r\n"]) as server, \
                self.assertRaises(plugin._Unavailable) as failure:
            self.get(server)
        self.assertEqual(str(failure.exception), "HTTP 503")
        with ScriptedServer([b"HTTP/1.1 302 Found\r\nLocation: /next.xml\r\nContent-Length: 0\r\n\r\n"]) as server, \
                self.assertRaises(plugin._Redirected) as moved:
            self.get(server)
        self.assertTrue(moved.exception.target.endswith("/next.xml"))

    def add_loopback_feed(self, name, url):
        with plugin._connect() as conn:
            conn.execute("INSERT INTO channels(name) VALUES (?)", (name,))
            conn.execute("INSERT INTO feeds(name,since,url) VALUES (?,?,?)", (name, plugin._window_start(), url))

    def test_slow_feed_keeps_its_complete_items_and_cannot_hold_the_other_source(self):
        document = rss((article("first"), 1, "First"), (article("second"), 2, "Second"))
        cut = document.index(b"</item>") + len(b"</item>")
        slow = [CHUNKED_HEAD + chunk(document[:cut]), b"5;ext="] + self.TRICKLE + [
            b"\r\n" + document[cut:cut + 5] + b"\r\n" + chunk(document[cut + 5:]) + b"0\r\n\r\n"]
        fast = [b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: %d\r\n\r\n%s"
                % (len(document), document)]
        with ScriptedServer(slow, self.GAP) as slow_server, ScriptedServer(fast) as fast_server, \
                patch.object(plugin, "READ_DEADLINE", self.DEADLINE):
            self.add_loopback_feed("rss:slow_feed", slow_server.url)
            self.add_loopback_feed("rss:fast_feed", fast_server.url)
            started = time.monotonic()
            result = self.call("fetch_posts")
        self.assertLess(time.monotonic() - started, self.PROMPTLY)
        rows = {row["channel"]: row for row in result["channels"]}
        self.assertEqual(len(rows["rss:fast_feed"]["posts"]), 2)
        self.assertFalse(rows["rss:fast_feed"]["feed_truncated"])
        self.assertEqual((rows["rss:slow_feed"]["coverage"], rows["rss:slow_feed"]["feed_truncated"],
                          [p["title"] for p in rows["rss:slow_feed"]["posts"]]), ("feed_window", True, ["First"]))

    def test_source_stuck_where_no_socket_can_be_cut_is_reported_and_the_others_returned(self):
        self.call("channels", action="add", name=OPENAI)
        self.call("channels", action="add", name="sample_news")
        release = threading.Event()
        self.addCleanup(release.set)
        unblock = threading.Timer(6, release.set)  # holds any fan-out that waits for every source
        unblock.daemon = True
        unblock.start()
        self.addCleanup(unblock.cancel)

        def lookup(host, _port):
            if host == "openai.com":  # a DNS lookup cannot be interrupted
                release.wait(15)
                raise socket.gaierror(8, "released by the test")
            return PUBLIC

        self.resolve.side_effect = lookup
        with patch.object(plugin, "FETCH_DEADLINE", 0.5, create=True), \
                patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            started = time.monotonic()
            result = self.call("fetch_posts")
            elapsed = time.monotonic() - started
            release.set()
            join_left_behind()
        self.assertLess(elapsed, 4)
        rows = {row["channel"]: row for row in result["channels"]}
        self.assertTrue(result["ok"])
        self.assertTrue(rows["sample_news"]["posts"])
        self.assertEqual(rows[key(OPENAI)]["coverage"], "timeout")
        self.assertIn("deadline", rows[key(OPENAI)]["error"])
        self.assertEqual(rows[key(OPENAI)]["posts"], [])

    def test_single_source_call_is_bounded_too(self):
        self.call("channels", action="add", name="sample_news")
        release = threading.Event()
        self.addCleanup(release.set)
        unblock = threading.Timer(6, release.set)
        unblock.daemon = True
        unblock.start()
        self.addCleanup(unblock.cancel)

        def stuck(_channel):
            release.wait(15)
            return [], "released by the test"

        with patch.object(plugin, "FETCH_DEADLINE", 0.3, create=True), patch.object(plugin, "_fetch_channel", side_effect=stuck):
            started = time.monotonic()
            result = self.call("fetch_posts", channel="sample_news")
            elapsed = time.monotonic() - started
            release.set()
            join_left_behind()
        self.assertLess(elapsed, 4)
        self.assertFalse(result["ok"])
        self.assertEqual(result["channels"][0]["coverage"], "timeout")

    def test_read_left_behind_cannot_use_up_a_loss_report_or_advance_anything(self):
        self.call("channels", action="add", name="sample_news")

        def page_of(*ids):
            return ([{"id": f"sample_news/{n}", "url": f"https://t.me/sample_news/{n}",
                      "text": str(n), "date": "", "links": []} for n in ids], "")

        with patch.object(plugin, "_fetch_channel", return_value=page_of(1, 2)):
            self.call("fetch_posts")
        release = threading.Event()
        self.addCleanup(release.set)
        unblock = threading.Timer(6, release.set)
        unblock.daemon = True
        unblock.start()
        self.addCleanup(unblock.cancel)

        def stuck(_channel):
            release.wait(15)
            return page_of(2, 3)

        with patch.object(plugin, "FETCH_DEADLINE", 0.3, create=True), patch.object(plugin, "_fetch_channel", side_effect=stuck):
            left_behind = self.call("fetch_posts")["channels"][0]
            release.set()
            join_left_behind()
        self.assertEqual(left_behind["coverage"], "timeout")
        with plugin._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0], 0)
        with patch.object(plugin, "_fetch_channel", return_value=page_of(2, 3)):
            current = self.call("fetch_posts")["channels"][0]
        self.assertEqual(current["lost_unattempted_now"], 1)
        self.assertEqual(self.watermark(), 0)

    def test_timed_out_source_cannot_be_recorded_from_that_fetch(self):
        self.call("channels", action="add", name="sample_news")
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            self.call("fetch_posts")
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(_channel):
            release.wait(15)
            return [], "released by the test"

        with patch.object(plugin, "FETCH_DEADLINE", 0.3, create=True), patch.object(plugin, "_fetch_channel", side_effect=stuck):
            result = self.call("fetch_posts")
            release.set()
            join_left_behind()
        receipt = self.call("record_attempt", day="2026-09-27", fetch_ids=[result["fetch_id"]])
        self.assertEqual(receipt["post_ids"], [])
        self.assertEqual(self.watermark(), 0)

    def test_simulated_trickle_of_valid_headers_cannot_outlast_the_deadline(self):
        # The reviewer's probe: each byte arrives within the 6 s inactivity timeout, so silence never ends the read.
        clock = [1000.0]
        timeouts = []
        script = b"HTTP/1.1 200 OK\r\n" + b"".join(b"X-Pad-%d: y\r\n" % n for n in range(90)) + b"\r\n"

        class Trickle(plugin.io.RawIOBase):
            offset = 0

            def readable(self):
                return True

            def readinto(self, buffer):
                clock[0] += 5.9
                buffer[0] = script[self.offset]
                self.offset += 1
                return 1

        class Socket:
            def makefile(self, _mode, buffering=None):
                return Trickle()

            def settimeout(self, value):
                timeouts.append(value)

        self.assertGreater(len(script) * 5.9, 148)
        with patch.object(plugin.time, "monotonic", side_effect=lambda: clock[0]):
            deadline = plugin._Deadline()
            response = plugin.http.client.HTTPResponse(plugin._TimedSocket(Socket(), deadline), method="GET")
            with self.assertRaises(TimeoutError):
                response.begin()
        self.assertLessEqual(clock[0] - 1000.0, plugin.READ_DEADLINE + 5.9)
        self.assertTrue(timeouts)
        self.assertTrue(all(0 < value <= plugin.REQUEST_TIMEOUT for value in timeouts))
        self.assertLess(min(timeouts), plugin.REQUEST_TIMEOUT)  # the last read is held to what is left

    def test_every_address_draws_on_one_deadline(self):
        clock = [1000.0]
        attempts = []

        def blackholed(target, timeout, _source):
            attempts.append((target, timeout))
            clock[0] += timeout
            raise TimeoutError("timed out")

        addresses = [f"203.0.113.{n}" for n in range(1, 40)]
        with patch.object(plugin.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(plugin.socket, "getaddrinfo", side_effect=AssertionError("looked up again")), \
                patch.object(plugin.socket, "create_connection", side_effect=blackholed):
            connection = plugin._TimedConnection("example.org", timeout=plugin.REQUEST_TIMEOUT, deadline=plugin._Deadline(),
                                                 addresses=addresses)
            with self.assertRaises(TimeoutError) as cut:
                connection.connect()
        self.assertEqual(str(cut.exception), "read deadline exceeded")
        self.assertLessEqual(clock[0] - 1000.0, plugin.READ_DEADLINE)
        self.assertEqual(len(attempts), plugin.READ_DEADLINE // plugin.REQUEST_TIMEOUT)


class GzipBodyTests(ConsumerCase):
    """Finding D: every gzip member is decoded or the body says it was not; input is never hoarded."""

    URL = "https://feeds.example.org/feed.xml"

    def get(self, body, limit):
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse(
                content=body, url=self.URL, content_type="application/xml", encoding="gzip")) as opened:
            try:
                return plugin._get(self.URL, plugin._FEED_ACCEPT, limit, html=False)
            finally:
                self.bytes_read = opened.return_value.offset

    def test_concatenated_feed_is_read_whole(self):
        self.call("channels", action="add", name=OPENAI)
        document = rss((article("a"), 1, "A"), (article("b"), 2, "B"), (article("c"), 3, "C"))
        third = len(document) // 3
        body = b"".join(gzip.compress(document[n:n + third]) for n in range(0, len(document), third))
        with patch.object(plugin._OPENER, "open", side_effect=route(
                {OPENAI: feed_response(body, encoding="gzip", chunk=97)})):
            source = self.call("fetch_posts")["channels"][0]
        self.assertEqual((source["coverage"], source["feed_truncated"], source["feed_items"]), ("feed_window", False, 3))
        self.assertEqual(len(source["posts"]), 3)

    def test_trailing_member_past_the_decoded_limit_is_reported_as_too_long(self):
        # The reviewer's probe: 570 decoded bytes, then members that do not fit a 1,024-byte limit.
        first = gzip.compress(b"a" * 570)
        rest = gzip.compress(bytes(range(256)) * 40)
        body, complete = self.get(first + rest, 1024)
        self.assertTrue(complete)
        self.assertEqual(len(body), 1025)  # limit+1 is how a caller learns the body did not fit
        self.assertEqual(body[:570], b"a" * 570)
        self.assertEqual(body[570:1025], (bytes(range(256)) * 40)[:455])
        self.assertLessEqual(self.bytes_read, len(first) + 64 * 1024)

    def test_retained_input_is_one_read_at_most(self):
        document = b"<rss/>" * 100
        body = gzip.compress(document) + gzip.compress(b"") * 15000
        self.assertGreater(len(body), 256 * 1024)
        retained = []
        original = zlib.decompressobj

        def tracked(*args):
            real = original(*args)

            class Probe:
                eof = property(lambda _self: real.eof)
                unused_data = property(lambda _self: real.unused_data)

                def decompress(self, data, max_length=0):
                    out = real.decompress(data, max_length)
                    retained.append(len(real.unused_data))
                    return out
            return Probe()

        with patch.object(plugin.zlib, "decompressobj", side_effect=tracked):
            decoded, complete = self.get(body, plugin.MAX_FEED_BYTES)
        self.assertEqual((decoded, complete), (document, True))
        self.assertLessEqual(max(retained), 64 * 1024)

    def test_compressed_input_past_its_bound_is_refused_without_reading_on(self):
        body = gzip.compress(b"x") + gzip.compress(b"") * 20000
        limit = 1024
        self.assertGreater(len(body), 2 * limit + 128 * 1024)
        with self.assertRaises(plugin._Unavailable) as refused:
            self.get(body, limit)
        self.assertEqual(str(refused.exception), "compressed body exceeds its input bound")
        self.assertLessEqual(self.bytes_read, 2 * limit + 128 * 1024)

    def test_zero_padding_is_not_data_and_other_trailing_bytes_are_not_a_member(self):
        document = b"<rss></rss>"
        self.assertEqual(self.get(gzip.compress(document) + b"\0" * 512, 1024), (document, True))
        self.assertEqual(self.get(gzip.compress(document) + b"\0" * 64 + gzip.compress(document), 1024),
                         (document + document, True))
        with self.assertRaises(plugin._Unavailable) as corrupt:
            self.get(gzip.compress(document) + b"not a gzip member", 1024)
        self.assertEqual(str(corrupt.exception), "corrupt gzip body")

    def test_truncated_later_member_is_not_a_complete_body(self):
        second = gzip.compress(b"second member")
        with self.assertRaises(plugin._Unavailable) as truncated:
            self.get(gzip.compress(b"first member") + second[:len(second) // 2], 1024)
        self.assertEqual(str(truncated.exception), "truncated gzip body")

    def test_single_member_and_empty_members_keep_working(self):
        document = b"<rss></rss>"
        self.assertEqual(self.get(gzip.compress(document), 1024), (document, True))
        self.assertEqual(self.get(gzip.compress(b"") + gzip.compress(document) + gzip.compress(b""), 1024), (document, True))
        with self.assertRaises(plugin._Unavailable):  # a declared gzip body with no bytes at all
            self.get(b"", 1024)


if __name__ == "__main__":
    unittest.main()
