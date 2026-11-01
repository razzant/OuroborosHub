"""Consumer-facing, offline tests for the public preview and owner-chosen feed extension."""
from __future__ import annotations

import email.utils
import gzip
import hashlib
import html
import importlib.util
import itertools
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


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
        for word in ("text_not_extracted", "baseline_posts", "omitted_posts", "redirect_to", "excerpt"):
            self.assertIn(word, text)

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
            revision = self.call("fetch_posts", include_attempted=True)["channels"][0]["posts"]
            self.assertEqual([post["id"] for post in revision], ["sample_news/12", "sample_news/13"])
            self.assertTrue(all(post["already_attempted"] for post in revision))

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
        content = (b'<div class="tgme_widget_message" data-post="sample_news/31">'
                   b'<a class="tgme_widget_message_reply" href="https://t.me/sample_news/5">'
                   b'<div class="tgme_widget_message_text js-message_reply_text">quoted older post</div></a>'
                   b'<div class="tgme_widget_message_text js-message_text">Own words</div></div>'
                   b'<div class="tgme_widget_message" data-post="sample_news/30">'
                   b'<div class="tgme_widget_message_text">Earlier</div></div>'
                   b'<div class="tgme_widget_message" data-post="sample_news/31">'
                   b'<div class="tgme_widget_message_text">Duplicate render</div></div>')
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
        content = b"".join((f'<div class="tgme_widget_message" data-post="sample_news/{n}">'
                            f'<div class="tgme_widget_message_text">Post {n}</div></div>').encode()
                           for n in range(1, 22))
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
        # A separately requested retry brings the attempted posts back while they are still public.
        retry = self.fetch_page(content, include_attempted=True)["channels"][0]
        self.assertEqual([p["id"] for p in retry["posts"]], ["sample_news/20", "sample_news/22", "sample_news/24"])
        self.assertEqual([p["id"] for p in retry["text_not_extracted"]], ["sample_news/21", "sample_news/23"])
        self.assertTrue(all(p["already_attempted"] for p in retry["posts"] + retry["text_not_extracted"]))

    def test_portion_of_only_text_not_extracted_posts_advances_without_model_ids(self):
        first = [post_html(30, "Readable"), post_html(31, extra=PHOTO), post_html(32, extra=PHOTO)]
        source = self.fetch_page(page(*first))["channels"][0]
        self.assertEqual([p["id"] for p in source["text_not_extracted"]], ["sample_news/31", "sample_news/32"])
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/30"])
        self.assertEqual(receipt["attempted_not_read"]["ids"], ["sample_news/31", "sample_news/32"])
        later = page(*first, post_html(33, extra=PHOTO), post_html(34, extra=PLACEHOLDER))
        result = self.fetch_page(later)
        source = result["channels"][0]
        self.assertTrue(result["ok"])  # the page is readable; only its new portion has no text
        self.assertEqual((source["posts"], [p["id"] for p in source["text_not_extracted"]]),
                         ([], ["sample_news/33", "sample_news/34"]))
        receipt = self.call("record_attempt", day="2026-09-28", post_ids=[])
        self.assertEqual(receipt["attempted_not_read"], {"count": 2, "ids": ["sample_news/33", "sample_news/34"]})
        self.assertEqual(self.watermark(), 34)
        self.assertEqual(textless(self.fetch_page(later)["channels"][0]), [])

    def test_unrecorded_readable_post_stops_the_derived_advance(self):
        content = page(post_html(40, "A"), post_html(41, extra=PHOTO), post_html(42, "B"), post_html(43, extra=PHOTO))
        self.fetch_page(content)
        refused = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/42"])
        self.assertIn("every earlier returned post", refused["error"])
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=["sample_news/40"])
        self.assertEqual(receipt["attempted_not_read"]["ids"], ["sample_news/41"])
        self.assertEqual(self.watermark(), 41)
        source = self.fetch_page(content)["channels"][0]
        self.assertEqual(([p["id"] for p in source["posts"]], [p["id"] for p in source["text_not_extracted"]]),
                         (["sample_news/42"], ["sample_news/43"]))

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
        ids = [p["id"] for row in result["channels"] for p in row["posts"]]
        receipt = self.call("record_attempt", day="2026-09-27", post_ids=ids)
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
        listed = self.call("channels")
        self.assertEqual(listed["feed_urls"], {"rss:openai_news": ""})
        with sqlite3.connect(path) as db:
            columns = {table: {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                       for table in ("observed", "telegram_reads", "feeds")}
            self.assertTrue({"lost_reported", "text_extracted"} <= columns["observed"])
            self.assertIn("since", columns["telegram_reads"])
            self.assertIn("url", columns["feeds"])
            self.assertEqual(db.execute("SELECT id,text_extracted FROM observed").fetchall(), [(12, 1)])
        with patch.object(plugin._OPENER, "open", return_value=FakeResponse()):
            result = self.call("fetch_posts")
        legacy = next(row for row in result["channels"] if row["channel"] == "rss:openai_news")
        self.assertEqual(legacy["coverage"], "unavailable")
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
        revision = self.source(self.fetch({OPENAI: feed}, include_attempted=True)[0])
        # A was pruned with the window moving past it; B and C are still attempted.
        self.assertEqual([p["id"] for p in revision["posts"]], ids[1:])
        self.assertTrue(all(p["already_attempted"] for p in revision["posts"]))
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


if __name__ == "__main__":
    unittest.main()
