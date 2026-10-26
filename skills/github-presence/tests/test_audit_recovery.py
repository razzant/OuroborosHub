"""Consumer regressions for whole-payload audit f2ead509; synthetic data only."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_transport import Case, CONFIG, HOST_IDENTITY, HUMAN, REPO, WRITER, event, issue
from gh_presence.collector import Collector
from gh_presence.config import validate
from gh_presence.events import mentions
from gh_presence.host import Host
from gh_presence.http import Response, TransportError
from gh_presence.outbox import Outbox, Sender
from gh_presence.store import Store, dump


def companion_module():
    spec = importlib.util.spec_from_file_location("audit_companion", Path(__file__).resolve().parents[1] / "scripts/companion.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecoveryCase(Case):
    def restart(self):
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.outbox = Outbox(self.store, CONFIG)
        self.sender = Sender(self.store, self.provider, self.host, CONFIG)


class ChildTraversalTests(RecoveryCase):
    def parent(self, stream, spec):
        # Same parent checkpoint reset performed by Collector.seed; children
        # are exclusively scheduled/consumed by Collector.tick.
        with self.store.transaction() as db:
            db.execute("DELETE FROM jobs WHERE stream=? AND state='done'", (stream,))
            self.store.job(db, stream, spec)

    def drain(self):
        collector = Collector(self.store, self.provider, CONFIG)
        for _ in range(40):
            if not self.store.one("SELECT id FROM jobs WHERE state='pending'"):
                return
            self.assertTrue(collector.tick(), self.store.rows("SELECT * FROM gaps WHERE resolved IS NULL"))
        self.fail("child traversal did not finish")

    @staticmethod
    def page(rows, cursor=None):
        return {"nodes": rows, "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor}}

    def repeat_discussions(self, audit):
        stream = "repo:321:" + ("audit:" if audit else "") + "discussions"
        header = {"id": "D1", "number": 1, "body": "old discussion", "createdAt": "2020-01-01T00:00:00Z"}
        comment = lambda name: {"id": name, "body": name, "createdAt": "2020-01-01T00:00:00Z"}
        self.store.set_meta("bootstrap:repo:321", "2026-01-01T00:00:00Z")
        pages = self.provider.graph_pages
        pages.update({("discussions", None, None): self.page([header]),
                      ("discussion_comments", "D1", None): self.page([comment("C1")], "comments-2"),
                      ("discussion_comments", "D1", "comments-2"): self.page([comment("C2")]),
                      ("discussion_replies", "C1", None): self.page([comment("R1")], "replies-2"),
                      ("discussion_replies", "C1", "replies-2"): self.page([comment("R2")]),
                      ("discussion_replies", "C2", None): self.page([])})
        spec = {"kind": "discussions", "repo": REPO, "audit": audit}
        self.parent(stream, spec)
        self.drain()
        original = self.store.rows("SELECT id,event FROM inbox ORDER BY seq")
        # Both changed child connections reuse their original continuation cursor.
        pages[("discussion_comments", "D1", "comments-2")]["nodes"].append(
            {"id": "C3", "body": "late comment", "createdAt": "2026-01-02T00:00:00Z"})
        pages[("discussion_replies", "C3", None)] = self.page([])
        pages[("discussion_replies", "C1", "replies-2")]["nodes"].append(
            {"id": "R3", "body": "late old-parent reply", "replyTo": {"id": "C1"}, "createdAt": "2026-01-02T00:00:00Z"})
        self.restart()
        self.parent(stream, spec)
        self.assertTrue(Collector(self.store, self.provider, CONFIG).tick())
        self.restart()  # unfinished children also survive a process boundary
        self.drain()
        after = self.store.rows("SELECT id,event FROM inbox ORDER BY seq")
        self.assertEqual(after[:len(original)], original)
        additions = [json.loads(row["event"]) for row in after[len(original):]]
        self.assertEqual({e["text"] for e in additions}, {"late comment", "late old-parent reply"})
        reply = next(e for e in additions if e["text"] == "late old-parent reply")
        self.assertEqual(reply["message"]["reply_route"]["reply_to"], "C1")
        self.parent(stream, spec)
        self.drain()
        self.assertEqual(self.store.rows("SELECT id,event FROM inbox ORDER BY seq"), after)

    def test_repeat_normal_discussion_comments_and_old_replies_after_restart(self):
        self.repeat_discussions(False)

    def test_repeat_audit_discussion_comments_and_old_replies_after_restart(self):
        self.repeat_discussions(True)

    def test_repeat_PR_reviews_and_unfinished_child_custody(self):
        self.provider.pages["/headers"] = Response(200, [{**issue(), "pull_request": {}}], {})
        first = "/repos/example/project/pulls/1/reviews?per_page=100"
        second = "https://api.github.com/repos/example/project/pulls/1/reviews?per_page=100&page=2"
        self.provider.pages[first] = Response(200, [{"id": 201, "body": "review-1"}], {"link": '<' + second + '>; rel="next"'})
        self.provider.pages[second] = Response(200, [{"id": 202, "body": "review-2"}], {})
        spec = {"kind": "issues", "repo": REPO, "path": "/headers"}
        stream = "repo:321:audit:issues"
        self.parent(stream, spec)
        collector = Collector(self.store, self.provider, CONFIG)
        self.assertTrue(collector.tick())
        self.assertTrue(collector.tick())
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=9999999999 WHERE json_extract(spec,'$.path')=?", (second,))
        unfinished = self.store.one("SELECT * FROM jobs WHERE json_extract(spec,'$.path')=?", (second,))
        self.parent(stream, spec)
        # Consume other siblings and the new header while that page is backed off.
        for _ in range(4):
            collector.tick()
        self.assertEqual(self.store.one("SELECT * FROM jobs WHERE id=?", (unfinished["id"],)), unfinished)
        self.assertFalse(self.store.one("SELECT id FROM jobs WHERE state='pending' AND json_extract(spec,'$.path')=?", (first,)))
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0")
        self.restart()
        self.drain()
        self.provider.pages[second].data.append({"id": 203, "body": "late review"})
        self.parent(stream, spec)
        self.drain()
        self.assertIsNotNone(self.store.one("SELECT id FROM inbox WHERE json_extract(event,'$.text')='late review'"))

    def test_real_REST_cycle_rolls_back_new_observation(self):
        spec = {"kind": "reviews", "repo": REPO, "number": 1, "path": "/first"}
        self.provider.pages["/first"] = Response(200, [{"id": 1, "body": "first"}], {"link": '</second>; rel="next"'})
        self.provider.pages["/second"] = Response(200, [{"id": 2, "body": "must roll back"}], {"link": '</first>; rel="next"'})
        self.parent("repo:321:issues:children", spec)
        collector = Collector(self.store, self.provider, CONFIG)
        self.assertTrue(collector.tick())
        before = self.store.rows("SELECT * FROM snapshots")
        self.assertFalse(collector.tick())
        self.assertEqual(self.store.rows("SELECT * FROM snapshots"), before)
        self.assertEqual(self.store.one("SELECT code FROM gaps WHERE resolved IS NULL")["code"], "pagination_cycle")

    def test_legacy_stalled_initial_child_repairs_on_parent_sweep_without_resetting_backoff(self):
        stream = "repo:321:discussions"
        spec = {"kind": "discussions", "repo": REPO}
        child = {"kind": "discussion_comments", "repo": REPO, "number": 1, "node_id": "D1", "discussion_id": "D1"}
        self.provider.graph_pages = {
            ("discussions", None, None): self.page([{"id": "D1", "number": 1}]),
            ("discussion_comments", "D1", None): self.page([], "next"),
            ("discussion_comments", "D1", "next"): self.page([])}
        with self.store.transaction() as db:
            self.store.job(db, stream + ":children", {**child, "cursor": "next"})
            db.execute("UPDATE jobs SET state='done'")
            self.store.job(db, stream + ":children", child)
            db.execute("UPDATE jobs SET due=9999999999 WHERE state='pending'")
        retained = self.store.one("SELECT * FROM jobs WHERE state='pending'")
        self.restart()
        self.parent(stream, spec)
        self.assertTrue(Collector(self.store, self.provider, CONFIG).tick())
        self.assertEqual(self.store.one("SELECT * FROM jobs WHERE id=?", (retained["id"],)), retained)
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0")
        self.drain()

    def test_current_graphql_cycle_keeps_visited_pages_after_parent_revisit(self):
        stream = "repo:321:discussions"
        spec = {"kind": "discussions", "repo": REPO}
        self.provider.graph_pages = {
            ("discussions", None, None): self.page([{"id": "D1", "number": 1}]),
            ("discussion_comments", "D1", None): self.page([], "one"),
            ("discussion_comments", "D1", "one"): self.page([], "two"),
            ("discussion_comments", "D1", "two"): self.page([], "one")}
        self.parent(stream, spec)
        collector = Collector(self.store, self.provider, CONFIG)
        for _ in range(3):
            self.assertTrue(collector.tick())
        self.assertFalse(collector.tick())
        saved = self.store.rows("SELECT * FROM jobs WHERE stream=? ORDER BY id", (stream + ":children",))
        self.parent(stream, spec)
        self.assertTrue(collector.tick())
        self.assertEqual(self.store.rows("SELECT * FROM jobs WHERE stream=? ORDER BY id", (stream + ":children",)), saved)
        self.restart()
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0")
        self.assertFalse(Collector(self.store, self.provider, CONFIG).tick())
        self.assertEqual(self.store.one("SELECT code FROM gaps WHERE resolved IS NULL")["code"], "pagination_cycle")


class NotificationsRecoveryTests(Case):
    def build(self, failure):
        module = companion_module()
        self.calls, self.identity_attempts = [], 0
        self.second_identity = HUMAN
        self.clock = 2000000000
        self.addCleanup(patch.stopall)
        patch("gh_presence.collector.time.time", side_effect=lambda: self.clock).start()
        def client(recipient):
            def request(method, path, data=None, headers=None):
                self.assertEqual(method, "GET")
                self.calls.append((recipient, path))
                if path == "/user":
                    if recipient == 22:
                        self.identity_attempts += 1
                        if self.identity_attempts == 1 and failure:
                            raise failure
                        return Response(200, self.second_identity, {})
                    return Response(200, WRITER, {})
                if path.startswith("/notifications?"):
                    if recipient == 22:
                        self.assertGreater(self.identity_attempts, 0)
                        self.assertEqual(self.second_identity["id"], 22)
                    return Response(200, [{"id": "N" + str(recipient), "repository": REPO,
                                           "subject": {"url": "https://api.github.com/repos/example/project/issues/1"}}], {})
                if path == "/repos/example/project":
                    return Response(200, {**REPO, "private": False, "has_discussions": False}, {})
                return Response(200, [], {})
            return SimpleNamespace(request=request, cache_identity=str(recipient))
        config = {**CONFIG, "watched_identities": [], "writer_notifications": True}
        with patch.object(module, "writer_http", return_value=client(11)), \
                patch.object(module, "provider_http", side_effect=lambda config, token, **kw: client(int(token))), \
                patch.object(module, "HTTP", return_value=SimpleNamespace(request=lambda *a: Response(200, HOST_IDENTITY, {}))):
            host, collector, _ = module.build(self.store, config, {
                "GITHUB_PRESENCE_WRITER_TOKEN": "11", "GITHUB_PRESENCE_NOTIFICATIONS_TOKEN": "22"})
        self.addCleanup(host.close)
        return collector

    def test_transient_identity_failure_recovers_in_same_collector_with_backoff(self):
        collector = self.build(TransportError("read_unavailable", retry_at=2000000180))
        collector.seed()
        for _ in range(12):
            collector.tick()
        self.assertEqual(self.identity_attempts, 1)
        self.assertFalse(any(r == 22 and p.startswith("/notifications") for r, p in self.calls))
        self.assertIsNotNone(self.store.meta("notification:11:N11"))
        self.assertIsNotNone(self.store.meta("scan:repo:321:comments").get("completed_at"))
        self.clock += 120
        collector.seed()
        for _ in range(12):
            collector.tick()
        self.assertEqual(self.identity_attempts, 1)
        self.clock += 61
        collector.seed()
        for _ in range(12):
            collector.tick()
        self.assertEqual(self.identity_attempts, 2)
        self.assertIsNotNone(self.store.meta("notification:22:N22"))
        self.assertFalse(self.store.rows("SELECT * FROM gaps WHERE resolved IS NULL AND code='read_unavailable'"))

    def test_mismatched_identity_stays_barred_even_if_provider_later_changes(self):
        collector = self.build(TransportError("credential_identity_mismatch"))
        for _ in range(3):
            collector.seed()
            for _ in range(12):
                collector.tick()
            self.clock += 3600
        self.assertEqual(self.identity_attempts, 1)
        self.assertFalse(any(r == 22 and p.startswith("/notifications") for r, p in self.calls))
        self.assertIsNotNone(self.store.meta("notification:11:N11"))


class ContinuationRecoveryTests(RecoveryCase):
    def transport(self, status="continuing", fail_ack=False):
        self.posts = []
        def request(method, path, data=None):
            if method == "GET":
                return Response(200, {"status": status, "work_ref": "shared"}, {})
            self.posts.append(copy.deepcopy(data))
            if fail_ack:
                raise TransportError("host_ack_unknown")
            return Response(200, {"status": "recorded"}, {})
        host = Host(SimpleNamespace(request=request), self.store, self.outbox)
        self.addCleanup(host.close)
        return host

    def test_equal_turn_and_continuation_refs_poll_queue_without_resetting_custody(self):
        host = self.transport()
        envelope = {"status": "continuing", "turn_ref": "shared", "continuation_ref": "shared", "delivery_reporting_version": 1}
        host.accept("event-1", envelope)
        self.add_event(event("event-2"))
        host.poll()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["transport_queue"]["events"][0]["source_event_id"], "event-2")
        before = self.store.one("SELECT * FROM refs WHERE ref='shared'")
        host.accept("event-1", envelope)
        self.assertEqual(self.store.one("SELECT * FROM refs WHERE ref='shared'"), before)
        self.assertEqual(before["kind"], "continuation")

    def test_legacy_work_role_repaired_on_poll_and_ack_loss_replays_exact_snapshot(self):
        host = self.transport(fail_ack=True)
        host.accept("event-1", {"status": "continuing", "turn_ref": "shared", "continuation_ref": "shared", "delivery_reporting_version": 1})
        with self.store.transaction() as db:
            db.execute("UPDATE refs SET kind='work'")  # retained pre-fix row
        self.add_event(event("event-2"))
        host.close()
        self.restart()
        host = self.transport(fail_ack=True)
        host.poll()
        self.assertEqual(len(self.posts), 1)
        snapshot = self.posts[0]
        host.close()
        self.restart()
        self.add_event(event("event-3"))
        host = self.transport()
        with self.store.transaction() as db:
            db.execute("UPDATE refs SET due=0")
        host.poll()
        self.assertEqual(self.posts, [snapshot])
        with self.store.transaction() as db:
            db.execute("UPDATE refs SET due=0")
        host.poll()
        self.assertEqual(len(self.posts[-1]["transport_queue"]["events"]), 2)
        self.assertEqual(self.store.one("SELECT reporting FROM refs")["reporting"], 1)

    def test_foreign_continuation_does_not_promote_work_and_terminal_does_not_observe(self):
        host = self.transport(status="completed")
        host.accept("event-1", {"status": "continuing", "turn_ref": "shared"})
        self.add_event(event("event-2"))
        host.accept("event-2", {"status": "continuing", "continuation_ref": "shared"})
        host.poll()
        self.assertEqual(self.posts, [])
        self.assertEqual(self.store.one("SELECT kind,event_id FROM refs"), {"kind": "work", "event_id": "event-1"})
        self.assertEqual(self.store.one("SELECT code FROM gaps")["code"], "host_reference_identity_conflict")


class UncertaintyReportingTests(RecoveryCase):
    def unavailable(self, *args):
        raise TransportError("read_unavailable")

    def report_values(self):
        return [json.loads(r["value"]) for r in self.store.rows("SELECT value FROM reports ORDER BY id")]

    def test_provider_read_failure_reports_unknown_then_exact_delivery_without_second_POST(self):
        self.provider.fail_post = True
        self.provider.store_post_before_failure = True
        self.send_tool()
        self.advance()
        snapshot = self.provider.thread_snapshot
        self.provider.thread_snapshot = self.unavailable
        self.sender.reconcile_uncertain()
        original = self.report_values()
        self.assertEqual([r["state"] for r in original], ["uncertain"])
        self.restart()
        self.sender.repair_reports()
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET due=0")
        self.sender.reconcile_uncertain()
        self.assertEqual(self.report_values(), original)
        received = []
        host = Host(SimpleNamespace(request=lambda method, path, data: received.append(copy.deepcopy(data)) or
                                     Response(200, {"recorded": True}, {})), self.store, self.outbox)
        self.addCleanup(host.close)
        host.reports()
        self.assertEqual(received, original)
        self.provider.thread_snapshot = snapshot
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET due=0")
        self.sender.reconcile_uncertain()
        self.assertEqual(self.report_values()[0], original[0])
        self.assertEqual({r["state"] for r in self.report_values()}, {"uncertain", "delivered"})
        self.assertEqual({r["part_id"] for r in self.report_values()}, {"0"})
        host.reports()
        self.assertEqual({r["state"] for r in received}, {"uncertain", "delivered"})
        self.assertFalse(self.advance())
        self.assertEqual(len(self.provider.posts), 1)

    def test_repair_known_uncertainty_after_interrupted_send_without_provider_reads(self):
        self.send_tool()
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET state='sending'")
            db.execute("UPDATE parts SET state='sending'")
        self.restart()
        self.provider.thread_snapshot = lambda *a: self.fail("repair must not read the provider")
        self.sender.repair_reports()
        self.assertEqual([r["state"] for r in self.report_values()], ["uncertain"])
        self.assertEqual(self.provider.posts, [])

    def test_legacy_unknown_closure_reports_during_read_outage_and_never_repeats_PATCH(self):
        self.send_tool(operation="close", state_reason="not_planned")
        self.advance()
        self.provider.fail_close = True
        self.advance()
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET report_due=0")  # pre-fix closure custody
        self.restart()
        self.provider.thread_snapshot = self.unavailable
        self.sender.repair_reports()
        reports = self.report_values()
        self.assertEqual({(r["part_id"], r["state"]) for r in reports}, {("0", "delivered"), ("status", "uncertain")})
        self.sender.reconcile_uncertain()
        self.assertEqual(self.report_values(), reports)
        self.assertFalse(self.advance())
        self.assertEqual(len(self.provider.posts), 1)
        self.assertEqual(len(self.provider.closures), 1)


class BotMentionTests(unittest.TestCase):
    def test_watched_bot_configuration_and_exact_mention_spans(self):
        identities = [{"id": 901, "login": "fixture-app[bot]"}, {"id": 902, "login": "fixture-app"}]
        config = validate({**CONFIG, "watched_identities": identities})
        text = "@fixture-app[bot] @FIXTURE-APP[BOT] @fixture-app `@fixture-app[bot]` <!-- @fixture-app[bot] -->\n```\n@fixture-app[bot]\n```"
        found = mentions(text, config["watched_identities"])
        self.assertEqual([r["user_id"] for r in found], [901, 901, 902])
        self.assertEqual([text[s["span"][0]:s["span"][1]] for s in found],
                         ["@fixture-app[bot]", "@FIXTURE-APP[BOT]", "@fixture-app"])

    def test_bot_parser_does_not_emit_human_prefix_or_partial_suffix(self):
        identities = [{"id": 901, "login": "fixture-app[bot]"}, {"id": 902, "login": "fixture-app"}]
        self.assertEqual(mentions("@fixture-app[bot]", identities)[0]["user_id"], 901)
        for text in ("@fixture-app[bot]-extra", "@fixture-app[bot]/team", "x@fixture-app[bot]", "@fixture-app[bot][bot]"):
            with self.subTest(text=text):
                self.assertEqual(mentions(text, identities), [])
        for login in ("fixture-app[bogus]", "fixture-app[bot]/team", "fixture-app[bot][bot]"):
            with self.subTest(login=login), self.assertRaises(ValueError):
                validate({**CONFIG, "watched_identities": [{"id": 901, "login": login}]})

    def test_markdown_brackets_preserve_human_mentions_and_full_bot_spans(self):
        identities = [{"id": 901, "login": "fixture-app[bot]"}, {"id": 902, "login": "fixture-app"}]
        text = "[@fixture-app](url) [@fixture-app[bot]](url) @fixture-app[note]"
        found = mentions(text, identities)
        self.assertEqual([r["user_id"] for r in found], [902, 901, 902])
        self.assertEqual([text[s["span"][0]:s["span"][1]] for s in found],
                         ["@fixture-app", "@fixture-app[bot]", "@fixture-app"])
