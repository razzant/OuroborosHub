"""Falsifying controls for the returned transport repair (synthetic data only)."""
import copy
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_transport import Case, CONFIG, REPO, WRITER, FakeProvider, Loopback, event, issue, route
from gh_presence.config import API, account_id, validate
from gh_presence.collector import Collector
from gh_presence.http import HTTP, Response, TransportError
from gh_presence.host import Host
from gh_presence.discovery import discovery_event, public_subject
from gh_presence.provider import GitHub
from gh_presence.store import Store


class RefusalTests(Case):
    def test_body_only_secondary_rate_refusal_retries_exact_intent(self):
        self.send_tool()
        original = self.store.one("SELECT intent FROM outbox")["intent"]
        api = GitHub(SimpleNamespace(request=lambda *a, **k: Response(
            403, {"message": "You have exceeded a secondary rate limit."}, {})), self.store)
        self.provider.send = api.send
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "held")
        self.assertEqual(self.store.one("SELECT state FROM parts")["state"], "queued")
        self.assertEqual(self.store.one("SELECT intent FROM outbox")["intent"], original)
        self.assertGreater(self.outbox.inspect("tool:request-1")["due"], time.time())

    def test_local_backoff_before_post_is_definitely_unsent(self):
        self.send_tool()
        api = GitHub(SimpleNamespace(request=lambda *a, **k: self.fail("must not call HTTP")), self.store)
        self.store.set_meta("quota:writer", {"retry_at": time.time() + 60})
        self.provider.send = api.send
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "held")
        self.assertEqual(self.store.one("SELECT state FROM parts")["state"], "queued")

    def test_definite_close_refusal_is_failed_not_unknown(self):
        self.send_tool(operation="close", state_reason="not_planned")
        self.advance()
        def refuse(*args):
            raise TransportError("closure_effect_unconfirmed", Response(403, {"message": "Forbidden"}, {}))
        self.provider.close_issue = refuse
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "failed")
        report = json.loads(self.store.one("SELECT value FROM reports WHERE id LIKE '%:status'")["value"])
        self.assertEqual(report["state"], "failed")

    def test_close_rate_refusal_preserves_explanation_and_retries_only_close(self):
        self.send_tool(operation="close", state_reason="not_planned")
        self.advance()
        original = self.provider.close_issue
        def refuse(*args):
            raise TransportError("closure_effect_unconfirmed", Response(403, {"message": "secondary rate limit"}, {}))
        self.provider.close_issue = refuse
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "held")
        self.provider.close_issue = original
        self.advance()
        self.assertEqual(len(self.provider.posts), 1)
        self.assertEqual(len(self.provider.closures), 1)


class RecoveryTests(Case):
    def test_multiple_exact_receipts_keep_unknown_effect_and_visible_gap(self):
        self.provider.fail_post = True
        self.send_tool()
        self.advance()
        part = self.store.one("SELECT * FROM parts")
        for identifier in (701, 702):
            self.provider.rows.append({"id": identifier, "user": WRITER, "body": part["body"],
                                       "issue_url": self.provider.http.base + "/repos/example/project/issues/1"})
        self.sender.reconcile_uncertain()
        gap = self.store.one("SELECT * FROM gaps WHERE scope='reconciliation:tool:request-1'")
        self.assertIsNone(gap["resolved"])
        self.assertEqual(gap["code"], "multiple_exact_provider_matches")
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "uncertain")
        self.assertEqual(len(self.provider.posts), 1)

    def test_failed_reconciliation_read_is_retried_without_reposting(self):
        self.provider.fail_post = True
        self.provider.store_post_before_failure = True
        self.send_tool()
        self.advance()
        snapshot = self.provider.thread_snapshot
        def unavailable(*args):
            raise TransportError("read_unavailable")
        self.provider.thread_snapshot = unavailable
        self.sender.reconcile_uncertain()
        self.provider.thread_snapshot = snapshot
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET due=0")
        self.sender.reconcile_uncertain()
        self.assertEqual(self.outbox.inspect("tool:request-1")["parts"][0]["state"], "delivered")
        self.assertEqual(len(self.provider.posts), 1)

    def test_later_exact_receipt_keeps_prior_unknown_report_immutable(self):
        self.provider.fail_post = True
        self.send_tool()
        self.advance()
        self.sender.reconcile_uncertain()
        original = self.store.rows("SELECT * FROM reports")
        part = self.store.one("SELECT * FROM parts")
        self.provider.rows.append({"id": 701, "user": WRITER, "body": part["body"],
                                   "issue_url": self.provider.http.base + "/repos/example/project/issues/1"})
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET due=0")
        self.sender.reconcile_uncertain()
        self.assertEqual(self.outbox.inspect("tool:request-1")["state"], "delivered")
        for r in original:
            self.assertEqual(self.store.one("SELECT value FROM reports WHERE id=?", (r["id"],))["value"], r["value"])
        self.assertTrue(any(json.loads(r["value"])["state"] == "delivered" for r in self.store.rows("SELECT value FROM reports")))
        self.assertEqual(len(self.provider.posts), 1)


class EfficiencyTests(Case):
    def test_first_scan_uses_explicit_old_watch_boundary_without_rewriting_it(self):
        config = {**CONFIG, "initial_since": "2026-01-01T00:00:00Z"}
        collector = Collector(self.store, self.provider, config)
        collector.seed()
        boundary = self.store.meta("bootstrap:repo:321")
        self.assertEqual(boundary, config["initial_since"])
        self.assertFalse(collector.prepare({"kind": "issues", "repo": REPO}, [issue()])[0][0][3])
        collector.config = {**config, "initial_since": "2026-10-23T00:00:00Z"}
        collector.seed()
        self.assertEqual(self.store.meta("bootstrap:repo:321"), boundary)

    def test_baseline_headers_do_not_read_whole_history(self):
        def forbidden(*args):
            self.fail("old baseline must not fetch every complete thread")
        self.provider.thread_snapshot = forbidden
        collector = Collector(self.store, self.provider, CONFIG)
        self.store.set_meta("bootstrap:repo:321", "2026-10-23T00:00:00Z")
        observations, _ = collector.prepare({"kind": "issues", "repo": REPO}, [issue(n) for n in range(1, 101)])
        self.assertEqual(len(observations), 100)
        self.assertTrue(all(row[3] for row in observations))

    def test_conditional_page_keeps_next_link_after_restart(self):
        class Client:
            cache_identity = "conditional-fixture"
            def __init__(self):
                self.headers = []
            def request(self, method, path, data=None, headers=None):
                self.headers.append(headers)
                if len(self.headers) == 1:
                    return Response(200, [issue()], {"etag": '"a"', "link": '</second>; rel="next"'})
                return Response(304, None, {})
        client = Client()
        provider = GitHub(client, self.store)
        first = provider.get("/first")
        self.store.close()
        self.store = Store(self.scratch.name)
        result = GitHub(client, self.store).get("/first")
        self.assertEqual(result, first)
        self.assertEqual(client.headers[-1], {"If-None-Match": '"a"'})

    def test_collection_reserve_does_not_block_send_readback(self):
        calls = []
        client = SimpleNamespace(request=lambda *a, **k: calls.append(a) or Response(200, WRITER, {}))
        provider = GitHub(client, self.store, config=CONFIG)
        self.store.set_meta("quota_resource:writer:core", {"remaining": 99, "reset": time.time() + 60})
        with provider.collection():
            with self.assertRaisesRegex(TransportError, "quota_reserve"):
                provider.get("/user")
        self.assertFalse(calls)
        provider.identity(WRITER["id"])
        self.assertEqual(len(calls), 1)

    def test_expired_http_cache_refetches_without_deleting_custody(self):
        calls = []
        client = SimpleNamespace(request=lambda method, path, data=None, headers=None:
                                 calls.append((path, headers)) or Response(200, issue(), {"etag": '"cached"'}))
        provider = GitHub(client, self.store)
        original = self.store.event("event-1")
        provider.get("/old-query")
        with self.store.transaction() as db:
            db.execute("UPDATE http_cache SET used=0")
        self.store.set_meta("cache_pruned_at", 0)
        provider.get("/new-query")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM http_cache")["n"], 1)
        provider.get("/old-query")
        self.assertIsNone(calls[-1][1])
        self.assertEqual(self.store.event("event-1"), original)

    def test_child_failure_does_not_hold_next_incremental_header_sweep(self):
        collector = Collector(self.store, self.provider, CONFIG)
        self.store.set_meta("scan:repo:321:issues", {"next_at": 0, "watermark": "2026-10-23T00:00:00+00:00"})
        with self.store.transaction() as db:
            self.store.job(db, "repo:321:issues:children", {"kind": "reviews", "repo": REPO, "number": 1, "path": "/unavailable"})
        collector.seed()
        job = self.store.one("SELECT spec FROM jobs WHERE stream='repo:321:issues' AND state='pending'")
        self.assertIn("since=", json.loads(job["spec"])["path"])
        self.assertIsNotNone(self.store.one("SELECT id FROM jobs WHERE stream='repo:321:issues:children'"))

    def test_removed_repository_job_cannot_read_provider(self):
        collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []})
        self.provider.get = lambda *_: self.fail("removed repository read")
        with self.store.transaction() as db:
            self.store.job(db, "repo:321:comments", {"kind": "comments", "repo": REPO, "path": "/old"})
        self.assertFalse(collector.tick())
        self.assertEqual(self.store.one("SELECT state FROM jobs")["state"], "removed")

    def test_status_is_noncreating_and_paginates_all_waiting_ids(self):
        absent = Path(self.scratch.name) / "never-created"
        store = Store(absent, readonly=True)
        self.assertEqual(store.status()["counts"]["inbox"], {})
        store.close()
        self.assertFalse(absent.exists())
        for n in range(105):
            self.add_event(event("waiting-" + str(n)))
        first = self.store.status()
        second = self.store.status(offset=first["pagination"]["next_offsets"]["waiting_events"])
        self.assertEqual(len(first["waiting_events"]) + len(second["waiting_events"]), 106)
        self.assertEqual(first["counts"]["inbox"]["ready"], 106)

    def test_old_parent_audit_finds_reply_despite_unchanged_header(self):
        collector = Collector(self.store, self.provider, CONFIG)
        head = {"id": "D1", "number": 1, "body": "old", "createdAt": "2020-01-01T00:00:00Z", "updatedAt": "2020-01-01T00:00:00Z"}
        parent = {"id": "C1", "body": "old parent", "createdAt": "2020-01-01T00:00:00Z"}
        def page(rows):
            return {"nodes": rows, "pageInfo": {"hasNextPage": False, "endCursor": None}}
        self.provider.graph_pages = {("discussions", None, None): page([head]),
            ("discussion_comments", "D1", None): page([parent]), ("discussion_replies", "C1", None): page([])}
        self.store.set_meta("bootstrap:repo:321", "2026-10-23T00:00:00Z")
        def traverse(stream, spec):
            with self.store.transaction() as db:
                self.store.job(db, stream, spec)
            for _ in range(10):
                if not self.store.one("SELECT id FROM jobs WHERE state='pending'"):
                    return
                self.assertTrue(collector.tick())
            self.fail("traversal did not finish")
        traverse("repo:321:discussions", {"kind": "discussions", "repo": REPO})
        self.provider.graph_pages[("discussion_replies", "C1", None)] = page([
            {"id": "R1", "body": "late reply", "replyTo": {"id": "C1"}, "createdAt": "2026-10-23T00:00:01Z"}])
        traverse("repo:321:fast", {"kind": "discussions", "repo": REPO, "cutoff": "2026-10-22T00:00:00Z"})
        self.assertFalse(self.store.one("SELECT id FROM inbox WHERE event LIKE '%late reply%'"))
        traverse("repo:321:audit:discussions", {"kind": "discussions", "repo": REPO, "audit": True})
        value = json.loads(self.store.one("SELECT event FROM inbox WHERE event LIKE '%late reply%'")["event"])
        self.assertEqual(value["message"]["reply_route"]["reply_to"], "C1")


class IntentVisibilityTests(Case):
    def test_stale_queue_ack_does_not_claim_current_intent_stored(self):
        self.send_tool()
        self.provider.rows.append({"id": 1555, "body": "Changed context"})
        self.advance()
        host = Host(SimpleNamespace(request=lambda *a: Response(200, {"status": "stale"}, {})), self.store, self.outbox)
        self.addCleanup(host.close)
        host.accept("event-1", {"status": "completed", "work_ref": "terminal-author", "outcome": "silent"}, ref="terminal-author")
        host.reconsiderations()
        self.assertEqual(self.store.one("SELECT code FROM gaps WHERE scope='reconsideration:tool:request-1'")["code"],
                         "held_intent_work_observation_pending")
        self.assertIsNone(self.store.meta("queue_report:terminal-author"))

    def test_terminal_work_gets_observation_without_a_wake_requirement(self):
        self.send_tool()
        self.provider.rows.append({"id": 1555, "body": "Changed context"})
        self.advance()
        seen = []
        server = Loopback(lambda method, path, data: (seen.append((method, path, data)) or (200, {"status": "recorded"}, {})))
        self.addCleanup(server.close)
        host = Host(HTTP(server.url, "synthetic", host=True), self.store, self.outbox)
        self.addCleanup(host.close)
        host.accept("event-1", {"status": "completed", "work_ref": "terminal-author", "outcome": "silent"}, ref="terminal-author")
        original = self.store.event("event-1")
        host.reconsiderations()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], "/presence/work/terminal-author")
        snapshot = seen[0][2]["transport_queue"]
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(snapshot["held_intents"][0]["source_event_id"], "event-1")
        self.assertEqual(self.store.event("event-1"), original)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM inbox")["n"], 1)
        self.assertIsNone(self.store.one("SELECT code FROM gaps WHERE scope='reconsideration:tool:request-1' AND resolved IS NULL"))


class DiscoveryTests(Case):
    def notification(self, private=False):
        return {"id": "N1", "reason": "mention", "updated_at": "2026-10-23T00:00:00Z",
                "repository": {**REPO, "private": private},
                "subject": {"type": "Issue", "url": "https://api.github.com/repos/example/project/issues/1"}}

    def test_public_discovery_keeps_two_recipient_sources_and_never_sends(self):
        config = {**CONFIG, "repositories": []}
        reader = FakeProvider()
        reader.pages["/notifications"] = Response(200, [self.notification()], {})
        collector = Collector(self.store, self.provider, config, {11: reader, 22: reader})
        with self.store.transaction() as db:
            for recipient in (11, 22):
                self.store.job(db, "notifications:" + str(recipient), {"kind": "notifications", "recipient": recipient, "path": "/notifications"})
        for _ in range(4):
            self.assertTrue(collector.tick())
        events = [json.loads(r["event"]) for r in self.store.rows("SELECT event FROM inbox WHERE id!='event-1'")]
        self.assertEqual({e["message"]["recipient_id"] for e in events}, {11, 22})
        self.assertEqual(len({e["source_event_id"] for e in events}), 2)
        host = Host(None, self.store, self.outbox)
        self.addCleanup(host.close)
        for e in events:
            self.assertEqual(e["facts"], ["any_message"])
            with self.assertRaisesRegex(ValueError, "discovery_has_no_write"):
                self.outbox.enqueue("discovery-test", e["source_event_id"], "Must not publish", {"kind": "tool"})
            host.accept(e["source_event_id"], {"status": "completed", "outcome": "message", "text": "Must not publish",
                "output_ref": e["source_event_id"], "delivery_reporting_version": 1})
        self.assertFalse(self.store.rows("SELECT * FROM outbox"))

    def test_discovery_preserves_missing_numeric_author_gap(self):
        self.provider.actor = lambda row: {"platform": "github", "attribution": "unknown"}
        repo, target, reply_route, history = public_subject(self.provider, self.notification())
        value = discovery_event("discovery-unknown", 1, None, config=CONFIG,
                                notification=self.notification(), recipient=11, repo=repo,
                                target=target, route=reply_route, history=history)
        self.assertFalse(value["conversation"]["history"]["complete"])
        self.assertEqual(value["message"]["coverage_gaps"], [
            {"scope": "history", "code": "history_actor_unavailable"},
            {"scope": "permission", "code": "repository_not_selected_for_writes"},
        ])
        self.assertEqual(value["facts"], ["any_message"])
        self.assertTrue(value["message"]["discovery_only"])

    def test_discovery_with_known_authors_keeps_complete_history(self):
        repo, target, reply_route, history = public_subject(self.provider, self.notification())
        value = discovery_event("discovery-known", 1, None, config=CONFIG,
                                notification=self.notification(), recipient=11, repo=repo,
                                target=target, route=reply_route, history=history)
        self.assertTrue(value["conversation"]["history"]["complete"])
        self.assertEqual(value["message"]["coverage_gaps"], [
            {"scope": "permission", "code": "repository_not_selected_for_writes"},
        ])

    def test_private_external_notification_never_fetches_subject(self):
        self.provider.repository = lambda *_: self.fail("private outside selection")
        with self.assertRaisesRegex(TransportError, "visibility_unproven"):
            public_subject(self.provider, self.notification(private=True))

    def test_removed_recipient_cannot_fetch_pending_discovery(self):
        self.provider.repository = lambda *_: self.fail("removed recipient must not fetch")
        collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, {})
        with self.store.transaction() as db:
            self.store.job(db, "notifications:22:children", {"kind": "notification_discovery", "recipient": 22,
                                                           "notification": self.notification()})
        self.assertFalse(collector.tick())
        self.assertEqual(self.store.one("SELECT state FROM jobs")["state"], "removed")
        self.assertEqual(self.store.one("SELECT code FROM gaps")["code"], "notification_recipient_removed")
        collector.notifications = {22: FakeProvider()}
        collector.seed()
        self.assertEqual(self.store.one("SELECT state FROM jobs WHERE stream='notifications:22:children'")["state"], "pending")

    def test_request_removal_and_readdition_do_not_retain_sticky_fact(self):
        collector = Collector(self.store, self.provider, CONFIG)
        spec = {"kind": "requests", "repo": REPO, "number": 1}
        values = []
        from gh_presence.events import make_event
        for users in ([WRITER], [], [WRITER]):
            observations, _ = collector.prepare(spec, [{"users": users, "teams": []}])
            with self.store.transaction() as db:
                for key, snapshot, kwargs, baseline in observations:
                    eid = self.store.observe(db, key, snapshot, lambda eid, rev, old: make_event(eid, rev, old, **kwargs))
            values.append(self.store.event(eid)["facts"])
        self.assertIn("review_requested", values[0])
        self.assertNotIn("review_requested", values[1])
        self.assertIn("review_requested", values[2])


class PortabilityTests(Case):
    def test_explicit_pat_server_namespace_and_private_selection(self):
        config = validate({**CONFIG, "api_base_url": "https://enterprise.invalid/api/v3",
                           "graphql_url": "https://enterprise.invalid/api/graphql", "web_base_url": "https://enterprise.invalid",
                           "api_version": "2022-11-28", "private_repository_ids": [321]})
        self.assertNotEqual(account_id(11), account_id(11, config["api_base_url"]))
        client = HTTP(config["api_base_url"], "synthetic", graphql_url=config["graphql_url"], api_version=config["api_version"])
        self.assertEqual(client.url("/graphql"), config["graphql_url"])
        self.assertEqual(client.url("/user"), config["api_base_url"] + "/user")
        with self.assertRaisesRegex(ValueError, "cross_origin"):
            client.url("https://other.invalid/api/v3/user")
        raw = {**REPO, "private": True, "visibility": "private"}
        provider = GitHub(SimpleNamespace(request=lambda *a, **k: Response(200, raw, {})), self.store, config=config)
        self.assertEqual(provider.repository(REPO)["id"], 321)
        provider.config = CONFIG
        with self.assertRaisesRegex(TransportError, "private_repository_not_selected"):
            provider.repository(REPO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
