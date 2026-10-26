from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gh_presence.collector import Collector
from gh_presence.config import account_id, read_config, validate
from gh_presence.events import comparison, context_fingerprint, is_echo, make_event, mentions
from gh_presence.host import Host, REQUIRED_HOST_VERSIONS, REQUIRED_FACTS
from gh_presence.http import HTTP, Response, TransportError
from gh_presence.outbox import Outbox, Sender
from gh_presence.provider import GitHub
from gh_presence.store import Store, digest, dump, now

REPO = {"id": 321, "full_name": "example/project"}
WRITER = {"id": 11, "login": "service-writer", "type": "User"}
HUMAN = {"id": 22, "login": "contributor", "type": "User"}
WATCHED = [{"id": 11, "login": "service-writer"}, {"id": 22, "login": "contributor"}]
CONFIG = validate({"enabled": True, "expected_writer_id": 11, "notifications_user_id": 22,
                   "watched_identities": WATCHED, "repositories": [REPO]})
HOST_IDENTITY = {"places_version": 1, "presence_continuation_version": 1, "presence_delivery_version": 1,
                 **REQUIRED_HOST_VERSIONS, "presence_wake_facts": sorted(REQUIRED_FACTS)}


def issue(number=1):
    return {"id": 100 + number, "number": number, "title": "Question", "body": "Source text",
            "user": HUMAN, "state": "open", "locked": False, "assignees": [],
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}


def route(kind="comment", number=1):
    result = {"repo": REPO, "thread_kind": "issue", "number": number, "kind": kind}
    if kind == "inline":
        result.update(thread_kind="pr", root_comment_id=500)
    if kind == "discussion":
        result.update(thread_kind="discussion", discussion_id="D1", reply_to="C1")
    return result


def event(eid="event-1", target=None, history=None):
    target = target or route()
    history = history or [issue()]
    return make_event(eid, 1, None, account=account_id(11), repo=REPO,
                      thread=target["thread_kind"] + ".1", kind="issue_comment",
                      raw={"id": 555, "body": "Hello @service-writer", "user": HUMAN,
                           "created_at": "2026-01-02T00:00:00Z"},
                      actor={"platform": "github", "platform_actor_id": "22", "is_bot": False},
                      route=target, watched=WATCHED, history=history)


class Loopback:
    def __init__(self, dispatch):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def run_request(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                data = json.loads(body) if body else None
                owner.requests.append((self.command, self.path, data, dict(self.headers)))
                status, value, headers = dispatch(self.command, self.path, data)
                encoded = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(encoded)
            do_GET = do_POST = do_PATCH = run_request
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class FakeProvider:
    credential_name = "writer"
    def __init__(self):
        self.http = type("Endpoint", (), {"base": "https://api.github.com"})()
        self.rows = [issue()]
        self.posts = []
        self.closures = []
        self.fail_post = False
        self.fail_close = False
        self.store_post_before_failure = False
        self.pages = {}
        self.graph_pages = {}
        self.graph_calls = []

    def identity(self, expected):
        assert expected == WRITER["id"]
        return WRITER

    def actor(self, raw):
        user = raw.get("user") or {}
        return {"platform": "github", "platform_actor_id": str(user["id"]), "is_bot": user.get("type") == "Bot"} if "id" in user else {"platform": "github", "attribution": "unknown"}

    def repository(self, spec):
        return {**REPO, "private": False, "has_discussions": True, "permissions": {"push": True}}

    def thread_snapshot(self, target):
        return self.repository(REPO), self.rows[0], copy.deepcopy(self.rows)

    def send(self, target, body):
        self.posts.append(body)
        row = {"id": 700 + len(self.posts), "user": WRITER, "body": body,
               "created_at": now(), "issue_url": self.http.base + "/repos/example/project/issues/1"}
        if self.fail_post:
            if self.store_post_before_failure:
                self.rows.append(row)
            raise TransportError("network_outcome_unknown")
        self.rows.append(row)
        return row

    def readback(self, target, row):
        return row

    def close_issue(self, target, reason):
        self.closures.append(reason)
        if self.fail_close:
            raise TransportError("network_outcome_unknown")
        self.rows[0].update(state="closed", state_reason=reason)
        return self.rows[0]

    def get(self, path):
        if path in self.pages:
            value = self.pages[path]
            if isinstance(value, Exception):
                raise value
            return value
        return Response(200, self.rows[0], {})

    def discussion_page(self, spec):
        self.graph_calls.append((spec["kind"], spec.get("node_id"), spec.get("cursor")))
        value = self.graph_pages[(spec["kind"], spec.get("node_id"), spec.get("cursor"))]
        if isinstance(value, Exception):
            raise value
        return value


class FakeHost:
    def __init__(self):
        self.state = "active"
        self.lookups = 0
    def active(self, intent):
        self.lookups += 1
        if self.state != "active":
            raise TransportError("place_" + self.state)


class Case(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="github-presence-test-")
        self.store = Store(self.scratch.name)
        self.provider = FakeProvider()
        self.outbox = Outbox(self.store, CONFIG)
        self.host = FakeHost()
        self.sender = Sender(self.store, self.provider, self.host, CONFIG)
        self.add_event(event())

    def tearDown(self):
        self.store.close()
        self.scratch.cleanup()

    def add_event(self, value):
        with self.store.transaction() as db:
            db.execute("INSERT INTO inbox(id,event,created) VALUES(?,?,?)", (value["source_event_id"], dump(value), now()))

    def send_tool(self, **extra):
        return self.outbox.tool(account_id=account_id(11), conversation_id="repo.321", thread_id="issue.1",
                                source_event_id="event-1", body="An explanation", request_id="request-1", **extra)

    def advance(self):
        self.store.set_meta("last_mutation_at", 0)
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET due=0")
        return self.sender.tick()


class IdentityTests(Case):
    def test_main_source_links_use_provider_message_and_thread_urls(self):
        collector = Collector(self.store, self.provider, CONFIG)
        self.provider.rows[0]["html_url"] = "https://github.com/example/project/issues/1"
        raw = {"id": 777, "body": "Context", "user": HUMAN, "created_at": now(),
               "issue_url": "https://api.github.com/repos/example/project/issues/1",
               "html_url": "https://github.com/example/project/issues/1#issuecomment-777"}
        observations, _ = collector.prepare({"repo": REPO, "kind": "comments"}, [raw])
        value = make_event("source-link", 1, None, **observations[0][2])
        self.assertEqual(value["message"]["url"], raw["html_url"])
        self.assertEqual(value["conversation"]["url"], self.provider.rows[0]["html_url"])

    def test_configuration_rejects_unsupported_and_same_notification_writer(self):
        for change in ({"auth_mode": "app"}, {"api_base_url": "https://enterprise.invalid/api/v3"},
                       {"notifications_user_id": 11}, {"secret": "never"}):
            with self.assertRaises(ValueError):
                validate({**CONFIG, **change})
        Path(self.scratch.name, "settings.json").write_text("broken")
        with self.assertRaisesRegex(ValueError, "settings_unreadable"):
            read_config(self.scratch.name)

    def test_exact_mentions_ignore_fences_code_html_and_other_logins(self):
        body = "@service-writer @service-writer-more `@service-writer`\n```x\n@service-writer\n```\n<!-- @service-writer -->\n@CONTRIBUTOR"
        evidence = mentions(body, WATCHED)
        self.assertEqual([e["user_id"] for e in evidence], [11, 22])
        for span in evidence:
            self.assertTrue(body[slice(*span["span"])].startswith("@"))

    def test_observation_revisions_survive_replay_and_a_b_a(self):
        with self.store.transaction() as db:
            a = self.store.observe(db, "object", {"comparison": {"body": "A"}}, lambda eid, *_: event(eid))
            repeat = self.store.observe(db, "object", {"comparison": {"body": "A"}, "timestamp": "later"}, lambda eid, *_: event(eid))
            b = self.store.observe(db, "object", {"comparison": {"body": "B"}}, lambda eid, *_: event(eid))
            again = self.store.observe(db, "object", {"comparison": {"body": "A"}}, lambda eid, *_: event(eid))
        self.assertIsNone(repeat)
        self.assertEqual([a.rsplit(":", 1)[1], b.rsplit(":", 1)[1], again.rsplit(":", 1)[1]], ["1", "2", "3"])

    def test_fact_history_bot_and_assignment_evidence(self):
        raw = {"id": 700, "body": "current", "created_at": "2026-02-02T00:00:00Z", "in_reply_to_id": 600,
               "assignees": [WRITER], "users": [HUMAN], "teams": [{"id": 9}]}
        history = [{"id": 600, "created_at": "2026-02-01T00:00:00Z", "_actor_id": "11"}]
        value = make_event("facts", 1, None, account=account_id(11), repo=REPO, thread="pr.1", kind="review_request",
                           raw=raw, actor={"platform": "github", "is_bot": True}, route=route("inline"),
                           watched=WATCHED, history=history)
        self.assertTrue({"my_thread", "reply_to_me", "assigned", "review_requested"}.issubset(value["facts"]))
        self.assertNotIn("mention", value["facts"])
        self.assertEqual(value["actor"]["attribution"], "unknown")
        self.assertEqual(value["message"]["coverage_gaps"][0]["code"], "team_membership_not_proven")
        value = make_event("bot", 1, None, account=account_id(11), repo=REPO, thread="issue.1", kind="issue_comment",
                           raw=raw, actor={"platform": "github", "is_bot": True}, route=route(), watched=WATCHED, history=[])
        self.assertIn("from_bot", value["facts"])

    def test_later_arrival_resolves_exact_subroute_and_freezes_request(self):
        second = event("later", route("inline"))
        self.add_event(second)
        self.outbox.tool(account_id=account_id(11), conversation_id="repo.321", thread_id="pr.1",
                         source_event_id="later", body="Inline reply", request_id="inline")
        saved = json.loads(self.store.one("SELECT intent FROM outbox WHERE id='tool:inline'")["intent"])
        self.assertEqual(saved["route"]["root_comment_id"], 500)
        with self.assertRaisesRegex(ValueError, "source_address_mismatch"):
            self.outbox.tool(account_id=account_id(11), conversation_id="repo.321", thread_id="issue.1",
                             source_event_id="later", body="wrong", request_id="inline")
        with self.assertRaisesRegex(ValueError, "request_identity_conflict"):
            self.outbox.tool(account_id=account_id(11), conversation_id="repo.321", thread_id="pr.1",
                             source_event_id="later", body="changed", request_id="inline")


class SenderTests(Case):
    def test_successful_post_with_failed_readback_never_reposts_or_closes(self):
        for status in (429, 403, 404):
            with self.subTest(status=status):
                self.provider.rows = [issue()]
                delivery = "readback:" + str(status)
                self.outbox.enqueue(delivery, "event-1", "Explanation", {"kind": "tool"},
                                    operation="close", reason="not_planned")
                def failed_readback(*args):
                    raise TransportError("readback_unavailable", Response(status, {}, {"retry-after": "7"}), time.time() + 7)
                self.provider.readback = failed_readback
                before_posts = len(self.provider.posts)
                self.advance()
                self.store.recover()
                self.advance()
                result = self.outbox.inspect(delivery)
                self.assertEqual(result["state"], "uncertain")
                self.assertEqual(result["parts"][0]["state"], "uncertain")
                self.assertEqual(result["detail"]["candidate_response"]["id"], self.provider.rows[-1]["id"])
                self.assertEqual(len(self.provider.posts), before_posts + 1)
                self.assertEqual(self.provider.closures, [])

    def test_explicit_rate_refusal_waits_without_calling_it_unknown(self):
        self.send_tool()
        def refused(*args):
            raise TransportError("rate_refusal", Response(429, {"message": "rate limit"}, {"retry-after": "7"}), time.time() + 7)
        self.provider.send = refused
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "held")
        self.assertEqual(self.store.one("SELECT state FROM parts")["state"], "queued")
        self.assertGreater(self.outbox.inspect("tool:request-1")["due"], time.time())

    def test_failed_explanation_holds_closure_and_repairs_failed_report(self):
        self.send_tool(operation="close", state_reason="not_planned")
        def refused(*args):
            raise TransportError("rejected", Response(403, {"message": "Forbidden"}, {}))
        self.provider.send = refused
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "failed")
        with self.store.transaction() as db:
            db.execute("DELETE FROM reports")
        self.sender.repair_reports()
        self.assertEqual(json.loads(self.store.one("SELECT value FROM reports")["value"])["state"], "failed")
        self.advance()
        self.assertEqual(self.provider.closures, [])

    def test_uncertain_closure_preserves_stage_and_never_repeats(self):
        self.send_tool(operation="close", state_reason="not_planned")
        self.advance()
        self.provider.fail_close = True
        self.advance()
        self.store.recover()
        self.sender.reconcile_uncertain()
        self.advance()
        self.assertEqual(len(self.provider.posts), 1)
        self.assertEqual(len(self.provider.closures), 1)
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "uncertain")
        receipt = json.loads(self.store.one("SELECT value FROM reports WHERE id LIKE '%:status'")["value"])
        self.assertEqual(receipt["state"], "uncertain")

    def test_silent_after_enqueue_and_later_delivery(self):
        self.send_tool()
        self.host.state = "silent"
        self.advance()
        self.assertEqual(self.provider.posts, [])
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "held")
        self.host.state = "active"
        self.advance()
        self.advance()
        self.assertEqual(len(self.provider.posts), 1)
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "delivered")
        self.send_tool()
        self.advance()
        self.assertEqual(len(self.provider.posts), 1)

    def test_close_only_after_confirmed_comment_then_readback(self):
        self.send_tool(operation="close", state_reason="not_planned")
        self.advance()
        self.assertEqual(len(self.provider.posts), 1)
        self.assertEqual(self.provider.closures, [])
        self.advance()
        self.assertEqual(self.provider.closures, ["not_planned"])
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "delivered")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM reports")["n"], 2)

    def test_unknown_comment_never_reposts_or_closes_after_restart(self):
        self.provider.fail_post = True
        self.send_tool(operation="close", state_reason="not_planned")
        self.advance()
        self.store.recover()
        self.send_tool(operation="close", state_reason="not_planned")
        for _ in range(3):
            self.advance()
        self.assertEqual(len(self.provider.posts), 1)
        self.assertEqual(self.provider.closures, [])
        self.sender.reconcile_uncertain()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "uncertain")

    def test_unknown_post_found_by_exact_author_route_marker_no_repost(self):
        self.provider.fail_post = True
        self.provider.store_post_before_failure = True
        self.send_tool(operation="close", state_reason="not_planned")
        self.advance()
        self.sender.reconcile_uncertain()
        self.advance()
        result = self.outbox.inspect("tool:request-1")
        self.assertEqual(result["parts"][0]["state"], "delivered")
        self.assertEqual(self.provider.closures, [])
        self.assertEqual(len(self.provider.posts), 1)

    def test_crash_after_mutation_begins_is_uncertain(self):
        self.send_tool()
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET state='sending'")
            db.execute("UPDATE parts SET state='sending'")
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.assertEqual(self.store.one("SELECT state FROM parts")["state"], "uncertain")

    def test_new_context_requires_deliberate_same_request_reconsideration(self):
        self.send_tool()
        self.provider.rows.append({"id": 1234, "body": "New relevant context", "user": HUMAN})
        self.advance()
        row = self.outbox.inspect("tool:request-1")
        self.assertEqual(row["state"], "needs_reconsideration")
        self.assertEqual(self.provider.posts, [])
        self.send_tool(reconsider_fingerprint=row["detail"]["current_fingerprint"])
        self.advance()
        self.assertEqual(len(self.provider.posts), 1)

    def test_long_output_all_characters_retained_in_order(self):
        body = "🦉abc" * 12000
        self.outbox.enqueue("output:large", "event-1", body, {"kind": "automatic", "source_event_id": "event-1"})
        rows = self.store.rows("SELECT * FROM parts ORDER BY part")
        self.assertGreater(len(rows), 1)
        self.assertEqual("".join(r["body"].rsplit("\n\n", 1)[0] for r in rows), body)

    def test_self_echo_requires_author_route_and_unchanged_body(self):
        self.send_tool()
        self.advance()
        raw = self.provider.rows[-1]
        self.assertTrue(is_echo(self.store, route(), raw, self.provider.actor(raw)))
        self.assertFalse(is_echo(self.store, route(), raw, {"platform_actor_id": "22"}))
        self.assertFalse(is_echo(self.store, route(number=2), raw, self.provider.actor(raw)))
        self.assertFalse(is_echo(self.store, route(), {**raw, "body": raw["body"] + " manual edit"}, self.provider.actor(raw)))

    def test_top_level_discussion_echo_uses_physical_parent(self):
        target = {**route("discussion"), "reply_to": None}
        self.add_event(event("discussion-head", target))
        self.outbox.enqueue("output:discussion", "discussion-head", "top-level answer", {"kind": "automatic"})
        part = self.store.one("SELECT * FROM parts")
        with self.store.transaction() as db:
            db.execute("UPDATE parts SET state='uncertain'")
        raw = {"id": "new-comment", "databaseId": 999, "body": part["body"], "replyTo": None}
        future_route = {**target, "reply_to": "new-comment"}
        self.assertTrue(is_echo(self.store, future_route, raw, {"platform_actor_id": "11"}))

    def test_receipt_report_repaired_independently(self):
        self.send_tool()
        self.advance()
        with self.store.transaction() as db:
            db.execute("DELETE FROM reports")
        self.sender.repair_reports()
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM reports")["n"], 1)
        self.advance()
        self.assertEqual(len(self.provider.posts), 1)


class CollectorTests(Case):
    def test_old_object_edited_during_first_scan_is_live_with_unknown_prior(self):
        collector = Collector(self.store, self.provider, CONFIG)
        self.store.set_meta("bootstrap:repo:321", "2026-10-23T00:00:00Z")
        raw = {**issue(), "updated_at": "2026-10-23T00:00:01Z",
               "body": "Current @service-writer text", "assignees": [WRITER]}
        observations, _ = collector.prepare({"repo": REPO, "kind": "issues"}, [raw])
        key, snapshot, kwargs, baseline = observations[0]
        with self.store.transaction() as db:
            eid = self.store.observe(db, key, snapshot, lambda eid, rev, old: make_event(eid, rev, old, **kwargs), baseline)
        self.assertEqual(self.store.one("SELECT state FROM inbox WHERE id=?", (eid,))["state"], "ready")
        value = self.store.event(eid)
        self.assertEqual(value["message"]["action"], "updated_before_first_observation")
        self.assertEqual(value["actor"]["attribution"], "unknown")
        self.assertIn("any_message", value["facts"])
        self.assertIn("my_thread", value["facts"])  # the verified opener is watched
        self.assertFalse({"new_issue", "mention", "assigned"} & set(value["facts"]))
        self.assertIn({"scope": "revision", "code": "prior_revision_unavailable"}, value["message"]["coverage_gaps"])
        self.assertIn(raw["body"], value["text"])

    def test_manual_edit_and_reversion_of_proven_echo_are_new_revisions(self):
        self.send_tool()
        self.advance()
        collector = Collector(self.store, self.provider, CONFIG)
        original = copy.deepcopy(self.provider.rows[-1])
        spec = {"repo": REPO, "kind": "comments"}
        states = []
        for raw in (original, {**original, "body": "Manual edit"}, original):
            observations, _ = collector.prepare(spec, [raw])
            with self.store.transaction() as db:
                for key, snapshot, kwargs, baseline in observations:
                    self.store.observe(db, key, snapshot, lambda eid, rev, old, kw=kwargs:
                                       make_event(eid, rev, old, **kw) if kw else None, baseline)
            states.append(self.store.one("SELECT state FROM inbox ORDER BY seq DESC")["state"])
        self.assertEqual(states, ["echo", "ready", "ready"])
        self.assertEqual(self.store.one("SELECT revision FROM snapshots")["revision"], 3)
        edited = json.loads(self.store.one("SELECT event FROM inbox ORDER BY seq DESC")["event"])
        self.assertEqual(edited["actor"]["attribution"], "unknown")

    def test_failed_discussions_do_not_prevent_other_stream_rescans(self):
        collector = Collector(self.store, self.provider, CONFIG)
        collector.seed()
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state='done' WHERE stream!='repo:321:discussions'")
        collector.seed()
        streams = {r["stream"] for r in self.store.rows("SELECT stream FROM jobs WHERE state='pending'")}
        self.assertEqual(streams, {"repo:321:issues", "repo:321:comments", "repo:321:inline", "repo:321:discussions"})

    def test_notifications_keep_recipient_and_reason_without_inventing_mention(self):
        reader = FakeProvider()
        notification = {"id": "N1", "reason": "mention", "updated_at": "2026-10-23T00:00:00Z",
                        "repository": REPO, "subject": {"type": "Issue", "url": "https://api.github.com/repos/example/project/issues/1"}}
        reader.pages["/notifications"] = Response(200, [notification], {"x-poll-interval": "90"})
        collector = Collector(self.store, self.provider, CONFIG, {22: reader})
        with self.store.transaction() as db:
            self.store.job(db, "notifications:22", {"kind": "notifications", "recipient": 22, "path": "/notifications"})
        collector.tick()
        observations, _ = collector.prepare({"repo": REPO, "kind": "issues"}, [issue()])
        key, snapshot, kwargs, baseline = observations[0]
        value = make_event("actual-object", 1, None, **kwargs)
        self.assertNotIn("mention", value["facts"])
        self.assertEqual(value["message"]["notification"][0]["recipient"], 22)
        self.assertEqual(value["account_id"], account_id(11))
        self.assertNotEqual(value["actor"]["platform_actor_id"], "11")
        self.assertEqual(self.store.meta("scan:repo:321:issues")["next_at"], 0)

    def test_page_restart_and_equal_timestamps_preserve_all_101(self):
        collector = Collector(self.store, self.provider, CONFIG)
        self.store.set_meta("bootstrap:repo:321", "2025-01-01T00:00:00Z")
        self.provider.pages["/page1"] = Response(200, [issue(n) for n in range(1, 101)],
                                                {"link": '</page2>; rel="next"'})
        self.provider.pages["/page2"] = Response(200, [issue(101)], {})
        with self.store.transaction() as db:
            self.store.job(db, "repo:321", {"kind": "issues", "repo": REPO, "path": "/page1"})
        collector.tick()
        self.store.close()
        self.store = Store(self.scratch.name)
        collector = Collector(self.store, self.provider, CONFIG)
        collector.tick()
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM snapshots")["n"], 101)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM inbox WHERE state='ready'")["n"], 102)
        self.assertIsNotNone(self.store.meta("scan:repo:321")["completed_at"])

    def test_every_discussion_parent_and_reply_page_including_old_parent(self):
        collector = Collector(self.store, self.provider, CONFIG)
        def page(nodes, cursor=None):
            return {"nodes": nodes, "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor}}
        head = {"id": "D1", "number": 1, "body": "old topic", "createdAt": "2020-01-01T00:00:00Z"}
        parents = [{"id": "C" + str(n), "body": "parent", "createdAt": "2020-01-01T00:00:00Z"} for n in range(101)]
        replies = [{"id": "R" + str(n), "body": "new reply", "replyTo": {"id": "C0"}, "createdAt": "2026-10-23T00:00:00Z"} for n in range(101)]
        self.provider.graph_pages = {("discussions", None, None): page([head]),
                                     ("discussion_comments", "D1", None): page(parents[:100], "after100"),
                                     ("discussion_comments", "D1", "after100"): page(parents[100:]),
                                     ("discussion_replies", "C0", None): page(replies[:100], "after100"),
                                     ("discussion_replies", "C0", "after100"): page(replies[100:])}
        for n in range(1, 101):
            self.provider.graph_pages[("discussion_replies", "C" + str(n), None)] = page([])
        with self.store.transaction() as db:
            self.store.job(db, "repo:321", {"kind": "discussions", "repo": REPO})
        while self.store.one("SELECT id FROM jobs WHERE state='pending'"):
            self.assertTrue(collector.tick())
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM snapshots")["n"], 203)
        self.assertIn(("discussion_replies", "C100", None), self.provider.graph_calls)
        last = self.store.one("SELECT event FROM inbox WHERE event LIKE '%R100%' ORDER BY seq DESC")
        self.assertEqual(json.loads(last["event"])["message"]["reply_route"]["reply_to"], "C0")

    def test_partial_graphql_never_advances_checkpoint(self):
        collector = Collector(self.store, self.provider, CONFIG)
        response = Response(200, {"data": {"repository": {}}, "errors": [{"type": "RATE_LIMITED"}]}, {})
        self.provider.graph_pages[("discussions", None, None)] = TransportError("graphql_partial_errors", response)
        with self.store.transaction() as db:
            self.store.job(db, "repo:321", {"kind": "discussions", "repo": REPO})
        self.assertFalse(collector.tick())
        self.assertEqual(self.store.one("SELECT state FROM jobs")["state"], "pending")
        self.assertEqual(self.store.one("SELECT code FROM gaps WHERE resolved IS NULL")["code"], "graphql_partial_errors")
        self.assertIsNotNone(self.store.meta("partial_page:1"))


class HostTests(Case):
    def test_old_protocol_versions_do_not_prove_full_watch_capabilities(self):
        advertised = {"places_version": 1, "presence_continuation_version": 1, "presence_delivery_version": 1}
        server = Loopback(lambda *_: (200, advertised, {}))
        self.addCleanup(server.close)
        host = Host(HTTP(server.url, "synthetic", host=True), self.store, self.outbox)
        self.addCleanup(host.close)
        with self.assertRaisesRegex(TransportError, "host_capability_gap"):
            host.negotiate()
        self.assertFalse(self.store.meta("host_capabilities")["ready"])
        self.assertIn("assigned", self.store.meta("host_capabilities")["missing"]["presence_wake_facts"])
        self.assertEqual(set(self.store.meta("host_capabilities")["missing"]["presence_versions"]), set(REQUIRED_HOST_VERSIONS))
        advertised.update(HOST_IDENTITY)
        host.negotiate()
        self.assertTrue(self.store.meta("host_capabilities")["ready"])
        advertised["presence_main_contact_version"] = "1"
        with self.assertRaisesRegex(TransportError, "host_capability_gap"):
            host.negotiate()
        self.assertFalse(self.store.meta("host_capabilities")["ready"])
        advertised.update(HOST_IDENTITY)
        del advertised["presence_work_output_version"]
        with self.assertRaisesRegex(TransportError, "host_capability_gap"):
            host.negotiate()
        self.assertEqual(self.store.meta("host_capabilities")["missing"]["presence_versions"], ["presence_work_output_version"])
        advertised["places_version"] = None
        with self.assertRaisesRegex(TransportError, "host_incompatible"):
            host.negotiate()
        self.assertFalse(self.store.meta("host_capabilities")["ready"])
        self.assertEqual(len(server.requests), 5)

    def test_retained_original_child_outputs_dedup_by_identity_not_text(self):
        host = Host(None, self.store, self.outbox)
        self.addCleanup(host.close)
        host.accept("event-1", {"disposition": "retained", "turn_ref": "original", "work_ref": "child",
                                "delivery_reporting_version": 1}, status=500)
        self.assertEqual(self.store.one("SELECT state FROM inbox WHERE id='event-1'")["state"], "retained")
        response = {"status": "failed", "work_ref": "original", "child_work_ref": "late-child",
                    "outcome": "silent", "text": "", "delivery_reporting_version": 1,
                    "outputs": [{"output_ref": "selection-A", "outcome": "message", "text": "Same words"}]}
        host.accept("event-1", response, ref="original")
        host.accept("event-1", response, ref="original")
        host.accept("event-1", {"status": "completed", "work_ref": "child", "output_ref": "selection-B",
                                "outcome": "message", "text": "Same words", "delivery_reporting_version": 1}, ref="child")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM outbox")["n"], 2)
        self.assertEqual(self.store.one("SELECT state FROM refs WHERE ref='late-child'")["state"], "pending")

    def test_missing_output_identity_retained_as_protocol_gap(self):
        host = Host(None, self.store, self.outbox)
        self.addCleanup(host.close)
        host.accept("event-1", {"status": "completed", "turn_ref": "original", "outcome": "message", "text": "answer",
                                "delivery_reporting_version": 1}, ref="original")
        self.assertEqual(self.store.one("SELECT state FROM refs")["state"], "protocol_gap")
        self.assertIsNone(self.store.one("SELECT id FROM outbox"))
        self.assertIn("answer", self.store.one("SELECT value FROM envelopes")["value"])

    def test_allowance_retry_and_heard_silent_differ(self):
        host = Host(None, self.store, self.outbox)
        self.addCleanup(host.close)
        original = self.store.event("event-1")
        host.accept("event-1", {"disposition": "retry", "code": "consciousness_allowance_exhausted", "retry_after": 9}, status=429)
        self.assertEqual(self.store.one("SELECT state FROM inbox")["state"], "ready")
        self.assertEqual(self.store.event("event-1"), original)
        host.accept("event-1", {"status": "completed", "outcome": "silent", "text": ""})
        self.assertEqual(self.store.one("SELECT state FROM inbox")["state"], "accepted")

    def test_loopback_consumer_allows_later_mail_while_first_turn_pending(self):
        first_arrived = threading.Event()
        later_arrived = threading.Event()
        def dispatch(method, path, data):
            if path == "/identity":
                return 200, HOST_IDENTITY, {}
            if path == "/presence/turn":
                eid = data["event"]["source_event_id"]
                if eid == "event-1":
                    first_arrived.set()
                    if not later_arrived.wait(3):
                        return 500, {"disposition": "retry"}, {}
                else:
                    later_arrived.set()
                return 200, {"status": "completed", "outcome": "silent", "text": ""}, {}
            raise AssertionError(path)
        server = Loopback(dispatch)
        self.addCleanup(server.close)
        host = Host(HTTP(server.url, "synthetic-skill-token", host=True), self.store, self.outbox)
        self.addCleanup(host.close)
        host.negotiate()
        host.dispatch()
        self.assertTrue(first_arrived.wait(2))
        self.add_event(event("arrival-2", route("inline")))
        host.dispatch()
        self.assertTrue(later_arrived.wait(2))
        deadline = time.time() + 3
        while host.inflight and time.time() < deadline:
            time.sleep(.02)
            host.dispatch()
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM inbox WHERE state='accepted'")["n"], 2)

    def test_report_ack_loss_retries_report_only(self):
        self.send_tool()
        self.advance()
        calls = []
        def dispatch(method, path, data):
            calls.append(data)
            return (503, {"disposition": "retry"}, {}) if len(calls) == 1 else (200, {"recorded": True, "duplicate": True}, {})
        server = Loopback(dispatch)
        self.addCleanup(server.close)
        host = Host(HTTP(server.url, "synthetic-skill-token", host=True), self.store, self.outbox)
        self.addCleanup(host.close)
        host.reports()
        with self.store.transaction() as db:
            db.execute("UPDATE reports SET due=0")
        host.reports()
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(self.provider.posts), 1)
        self.assertEqual(self.store.one("SELECT state FROM reports")["state"], "acknowledged")


class HTTPTests(Case):
    def test_issue_transfer_does_not_redirect_a_frozen_route(self):
        def dispatch(method, path, data):
            if path == "/repos/example/project":
                return 200, {**REPO, "private": False}, {}
            return 200, {**issue(), "url": server.url + "/repos/elsewhere/other/issues/1"}, {}
        server = Loopback(dispatch)
        self.addCleanup(server.close)
        provider = GitHub(HTTP(server.url, "synthetic-writer", test_loopback=True), self.store)
        with self.assertRaisesRegex(TransportError, "issue_moved"):
            provider.thread_snapshot(route())

    def test_cross_origin_read_redirect_never_carries_the_credential(self):
        server = Loopback(lambda *_: (302, {}, {"Location": "https://attacker.invalid/steal"}))
        self.addCleanup(server.close)
        provider = GitHub(HTTP(server.url, "synthetic-writer", test_loopback=True), self.store)
        with self.assertRaisesRegex(ValueError, "cross_origin"):
            provider.get("/user")
        self.assertEqual(len(server.requests), 1)

    def test_complete_real_http_consumer_observation_reply_receipt_and_host_ack(self):
        comments, deliveries, turns = [], [], []
        def github_dispatch(method, path, data):
            bare = urlsplit(path).path
            if method == "GET" and bare == "/user":
                return 200, WRITER, {}
            if method == "GET" and bare == "/repos/example/project":
                return 200, {**REPO, "private": False, "permissions": {"push": True}}, {}
            if method == "GET" and bare == "/repos/example/project/issues/1":
                return 200, {**issue(), "url": github.url + bare}, {}
            if bare == "/repos/example/project/issues/1/comments":
                if method == "GET":
                    return 200, comments, {}
                row = {"id": 909, "body": data["body"], "user": WRITER, "issue_url": github.url + "/repos/example/project/issues/1"}
                comments.append(row)
                return 201, row, {}
            if bare == "/repos/example/project/issues/comments/909":
                return 200, comments[0], {}
            raise AssertionError((method, path))
        github = Loopback(github_dispatch)
        self.addCleanup(github.close)
        def host_dispatch(method, path, data):
            if path == "/identity":
                return 200, HOST_IDENTITY, {}
            if path == "/presence/turn":
                turns.append(data)
                return 200, {"status": "completed", "outcome": "message", "text": "Answer from the author",
                             "output_ref": "real-selection", "delivery_reporting_version": 1}, {}
            if path.startswith("/presence/place?"):
                return 200, {"place_id": "fixture-place", "state": "active", "label": "Synthetic"}, {}
            if path == "/presence/delivery":
                deliveries.append(data)
                return 200, {"recorded": True, "duplicate": False}, {}
            raise AssertionError(path)
        host_server = Loopback(host_dispatch)
        self.addCleanup(host_server.close)
        provider = GitHub(HTTP(github.url, "synthetic-writer", test_loopback=True), self.store)
        host = Host(HTTP(host_server.url, "synthetic-host-token", host=True), self.store, self.outbox)
        self.addCleanup(host.close)
        host.negotiate()
        host.dispatch()
        deadline = time.time() + 3
        while host.inflight and time.time() < deadline:
            time.sleep(.01)
            host.dispatch()
        sender = Sender(self.store, provider, host, CONFIG)
        self.assertTrue(sender.tick())
        host.reports()
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(comments), 1)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["message"]["provider_id"], 909)
        self.assertEqual(deliveries[0]["state"], "delivered")
        self.assertEqual(deliveries[0]["text"], comments[0]["body"])
        self.assertTrue(all("X-Skill-Token" not in headers for _, _, _, headers in github.requests))
        self.assertTrue(all("Authorization" not in headers for _, _, _, headers in host_server.requests))

    def test_credential_confinement_and_get_only_notifications(self):
        client = HTTP("https://api.github.com", "synthetic-read-token", readonly=True)
        with self.assertRaisesRegex(ValueError, "get_only"):
            client.request("POST", "/notifications", {})
        with self.assertRaisesRegex(ValueError, "cross_origin"):
            client.url("https://attacker.invalid/notifications")
        with self.assertRaises(ValueError):
            HTTP("http://localhost:9999", "synthetic", host=True)

    def test_real_graphql_client_pages_and_nested_reply_shape(self):
        seen = []
        def dispatch(method, path, data):
            self.assertEqual(path, "/graphql")
            seen.append(data)
            after = data["variables"].get("after")
            nodes = [{"id": "C" + str(n)} for n in range(100)] if not after else [{"id": "C100"}]
            return 200, {"data": {"node": {"replies": {"nodes": nodes, "pageInfo": {"hasNextPage": not bool(after), "endCursor": "cursor100"}}},
                                  "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": now()}}}, {}
        server = Loopback(dispatch)
        self.addCleanup(server.close)
        provider = GitHub(HTTP(server.url, "synthetic-writer", test_loopback=True), self.store)
        rows = list(provider.discussion_all({"kind": "discussion_replies", "node_id": "old-parent"}))
        self.assertEqual(len(rows), 101)
        self.assertEqual(seen[1]["variables"]["after"], "cursor100")
        self.assertIn("... on DiscussionComment", seen[0]["query"])
        self.assertNotIn("X-Skill-Token", server.requests[0][3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
