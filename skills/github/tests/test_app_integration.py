"""Parent integration qualification; copied beside skill tests by qualify_integration.py."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_auth
from test_transport import FakeHost, HOST_IDENTITY, HUMAN, REPO, issue, route
from gh_presence.collector import Collector
from gh_presence.config import connection_account, validate
from gh_presence.events import make_event
from gh_presence.http import Response, TransportError
from gh_presence.outbox import Outbox, Sender
from gh_presence.store import Store, dump, now


class IntegrationTests(unittest.TestCase):
    setUpClass = classmethod(test_auth.AppTests.setUpClass.__func__)
    setUp = test_auth.AppTests.setUp
    tearDown = test_auth.AppTests.tearDown
    client = test_auth.AppTests.client

    def prepare(self, operation="comment"):
        self.config["enabled"] = True
        self.target = {**issue(), "url": self.server.url + "/repos/example/project/issues/1"}
        self.comments, self.posts, self.closures = [], [], []
        self.server.overrides["GET", "/repos/example/project/issues/1"] = lambda *_: (200, self.target, {})
        self.server.overrides["GET", "/repos/example/project/issues/1/comments?per_page=100"] = lambda *_: (200, self.comments, {})
        def post(method, path, data):
            self.posts.append(data["body"])
            comment = {"id": 701, "user": self.server.bot, "body": data["body"], "issue_url": self.target["url"]}
            self.comments.append(comment)
            return 201, comment, {}
        self.server.overrides["POST", "/repos/example/project/issues/1/comments"] = post
        self.server.overrides["GET", "/repos/example/project/issues/comments/701"] = lambda *_: (200, self.comments[0], {})
        def close(method, path, data):
            self.closures.append(data)
            self.target.update(data)
            return 200, self.target, {}
        self.server.overrides["PATCH", "/repos/example/project/issues/1"] = close
        value = make_event("source-app", 1, None, account=connection_account(self.config), repo=REPO,
                           thread="issue.1", kind="issue_comment", raw={"id": 44, "body": "source", "user": HUMAN},
                           actor={"platform": "github", "platform_actor_id": str(HUMAN["id"])}, route=route(),
                           watched=[], history=[copy.deepcopy(self.target)])
        with self.store.transaction() as db:
            db.execute("INSERT INTO inbox(id,event,created) VALUES(?,?,?)", ("source-app", dump(value), now()))
        self.outbox = Outbox(self.store, self.config)
        self.outbox.tool(account_id=connection_account(self.config), conversation_id="repo.321", thread_id="issue.1",
                         source_event_id="source-app", body="Synthetic explanation", request_id="app-intent",
                         operation=operation, **({"state_reason": "not_planned"} if operation == "close" else {}))
        self.sender = Sender(self.store, self.provider, FakeHost(), self.config)

    def advance(self):
        self.store.set_meta("last_mutation_at", 0)
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET due=0")
        return self.sender.tick()

    def test_collector_uses_installation_namespace(self):
        collector = Collector(self.store, self.provider, self.config)
        self.assertEqual(collector.account, connection_account(self.config))
        self.assertNotIn(".user.", collector.account)

    def test_watched_App_bot_build_and_numeric_history_facts_use_encoded_lookup(self):
        from test_audit_recovery import companion_module
        selected = {"id": 901, "login": "fixture-app[bot]"}
        config = validate({"auth_mode": "app", "app_id": 77, "installation_id": 88,
                           "app_client_id": "Iv.fixture-client", "expected_writer_id": 901,
                           "repositories": [REPO], "watched_identities": [selected]})
        config.update(api_base_url=self.server.url, graphql_url=self.server.url + "/graphql")
        module = companion_module()
        with patch.object(module, "writer_http", return_value=self.http), \
                patch.object(module, "HTTP", return_value=SimpleNamespace(request=lambda *a: Response(200, HOST_IDENTITY, {}))):
            host, collector, _ = module.build(self.store, config, {})
        self.addCleanup(host.close)
        collision = {"id": 903, "login": "fixture-app", "type": "User"}
        self.server.overrides["GET", "/users/fixture-app"] = (200, collision, {})
        self.server.overrides["GET", "/users/contributor"] = (200, HUMAN, {})
        collector.provider.actor({"author": {"login": "fixture-app", "__typename": "User"}})
        parent = {"id": "C1", "databaseId": 601, "body": "bot history", "createdAt": "2026-01-01T00:00:00Z",
                  "author": {"login": "fixture-app", "__typename": "Bot"}}
        reply = {"id": "R1", "body": "@fixture-app[bot] a reply", "createdAt": "2026-01-02T00:00:00Z",
                 "replyTo": {"id": "C1"}, "author": {"login": HUMAN["login"], "__typename": "User"}}
        def graph(method, path, data):
            if "repository { databaseId }" in data["query"]:
                node = {"id": "D1", "number": 1, "repository": {"databaseId": 321}, "author": reply["author"]}
            elif data["variables"]["id"] == "D1":
                node = {"comments": {"nodes": [parent], "pageInfo": {"hasNextPage": False}}}
            else:
                node = {"replies": {"nodes": [reply], "pageInfo": {"hasNextPage": False}}}
            return 200, {"data": {"node": node}}, {}
        self.server.overrides["POST", "/graphql"] = graph
        spec = {"kind": "discussion_replies", "repo": REPO, "number": 1, "discussion_id": "D1", "node_id": "C1"}
        with self.store.transaction() as db:
            self.store.job(db, "repo:321:discussions:children", spec)
        self.assertTrue(collector.tick())
        value = json.loads(self.store.one("SELECT event FROM inbox")["event"])
        self.assertTrue({"my_thread", "reply_to_me", "mention"}.issubset(value["facts"]))
        evidence = value["message"]["fact_evidence"]
        self.assertEqual(evidence["reply_to_me"], {"parent_id": "C1", "actor_id": "901"})
        self.assertEqual(evidence["my_thread"][0]["actor_id"], "901")
        self.assertEqual(evidence["mention"][0]["user_id"], 901)
        self.assertIn(".app.77.installation.88", value["account_id"])
        self.assertEqual(value["message"]["raw"], reply)
        self.assertTrue(value["conversation"]["history"]["complete"])
        self.assertEqual(value["conversation"]["history"]["rows"][1]["author"], parent["author"])
        paths = [r[1] for r in self.server.calls]
        self.assertIn("/users/fixture-app%5Bbot%5D", paths)
        self.assertNotIn("/users/fixture-app[bot]", paths)
        # Same spelling, different verified numeric actor cannot imply history.
        self.server.overrides["GET", "/users/fixture-app%5Bbot%5D"] = (200, {**self.server.bot, "id": 902}, {})
        collector.provider.users.clear()
        observations, _ = collector.prepare(spec, [{**reply, "id": "R2", "body": "another reply"}])
        changed = make_event("second-bot-source", 1, None, **observations[0][2])
        self.assertFalse({"my_thread", "reply_to_me", "mention"} & set(changed["facts"]))

    def prepare_discussion(self):
        self.config["enabled"] = True
        self.config["watched_identities"] = [{"id": 901, "login": "fixture-app[bot]"}]
        self.server.permissions["discussions"] = "write"
        self.server.overrides["GET", "/users/fixture-app"] = (
            200, {"id": 903, "login": "fixture-app", "type": "User"}, {})
        self.server.overrides["GET", "/users/contributor"] = (200, HUMAN, {})
        self.server.overrides["GET", "/users/third-party%5Bbot%5D"] = (
            200, {"id": 904, "login": "third-party[bot]", "type": "Bot"}, {})
        self.server.overrides["GET", "/users/third-party"] = (404, {"message": "Not Found"}, {})
        self.target = {"id": "D1", "databaseId": 501, "number": 1, "title": "Discussion", "body": "Source",
                       "repository": {"databaseId": 321}, "createdAt": "2026-01-01T00:00:00Z",
                       "author": {"login": HUMAN["login"], "__typename": "User"}}
        self.parent = {"id": "C1", "databaseId": 601, "body": "A question from the colliding human",
                       "createdAt": "2026-01-02T00:00:00Z", "replyTo": None,
                       "author": {"login": "fixture-app", "__typename": "User"}}
        self.replies, self.posts = [], []
        def graph(method, path, data):
            query, variables = data["query"], data["variables"]
            if query.startswith("mutation"):
                args = variables["input"]
                self.assertEqual((args["discussionId"], args["replyToId"]), ("D1", "C1"))
                self.posts.append(args["body"])
                reply = {"id": "R1", "databaseId": 701, "body": args["body"],
                         "createdAt": "2026-01-03T00:00:00Z", "replyTo": {"id": "C1"},
                         "discussion": {"id": "D1"}, "author": {"login": "fixture-app", "__typename": "Bot"}}
                self.replies.append(reply)
                return 200, {"data": {"addDiscussionComment": {"comment": reply}}}, {}
            if "discussion { id }" in query:
                node = next(r for r in self.replies if r["id"] == variables["id"])
            elif "repository { databaseId }" in query:
                node = self.target
            else:
                field, rows = ("comments", [self.parent]) if variables["id"] == "D1" else ("replies", self.replies)
                node = {field: {"nodes": rows, "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            return 200, {"data": {"node": node}}, {}
        self.server.overrides["POST", "/graphql"] = graph
        self.collector = Collector(self.store, self.provider, self.config)
        self.spec = {"kind": "discussion_comments", "repo": REPO, "number": 1, "discussion_id": "D1", "node_id": "D1"}
        observations, _ = self.collector.prepare(self.spec, [self.parent])
        value = make_event("source-discussion", 1, None, **observations[0][2])
        with self.store.transaction() as db:
            db.execute("INSERT INTO inbox(id,event,created) VALUES(?,?,?)", ("source-discussion", dump(value), now()))
        self.outbox = Outbox(self.store, self.config)
        self.outbox.tool(account_id=connection_account(self.config), conversation_id="repo.321", thread_id="discussion.1",
                         source_event_id="source-discussion", body="Synthetic App reply", request_id="discussion-intent")
        self.sender = Sender(self.store, self.provider, FakeHost(), self.config)

    def test_bare_Bot_discussion_readback_confirms_numeric_author_without_repost(self):
        self.prepare_discussion()
        original = self.store.event("source-discussion")
        self.advance()
        result = self.outbox.inspect("tool:discussion-intent")
        self.assertEqual(result["parts"][0]["state"], "delivered")
        self.assertEqual(result["parts"][0]["receipt"]["author_id"], "901")
        self.assertEqual(result["parts"][0]["receipt"]["provider_id"], 701)
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:discussion-intent")["state"], "delivered")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.store.event("source-discussion"), original)

    def test_bare_Bot_discussion_echo_retained_while_human_and_other_bot_stay_live(self):
        self.prepare_discussion()
        self.advance()
        raw = copy.deepcopy(self.replies[0])
        self.replies.extend([{**raw, "id": "R2", "databaseId": 702, "author": self.parent["author"]},
                             {**raw, "id": "R3", "databaseId": 703,
                              "author": {"login": "third-party", "__typename": "Bot"}}])
        spec = {**self.spec, "kind": "discussion_replies", "node_id": "C1"}
        with self.store.transaction() as db:
            self.store.job(db, "repo:321:discussions:replies", spec)
        self.assertTrue(self.collector.tick())
        rows = self.store.rows("SELECT state,event FROM inbox WHERE id!='source-discussion' ORDER BY seq")
        self.assertEqual([row["state"] for row in rows], ["echo", "ready", "ready"])
        events = [json.loads(row["event"]) for row in rows]
        self.assertEqual([e["actor"]["platform_actor_id"] for e in events], ["901", "903", "904"])
        self.assertEqual(["from_bot" in e["facts"] for e in events], [True, False, True])
        self.assertEqual([e["message"]["raw"] for e in events], self.replies)
        self.assertEqual(len(self.posts), 1)

    def test_unverified_Bot_discussion_history_keeps_rows_and_truthful_gap(self):
        self.prepare_discussion()
        self.parent["author"] = {"login": "fixture-app", "__typename": "Bot"}
        self.server.overrides["GET", "/users/fixture-app%5Bbot%5D"] = (404, {"message": "Not Found"}, {})
        reply = {"id": "R1", "databaseId": 701, "body": "A reply", "replyTo": {"id": "C1"},
                 "createdAt": "2026-01-03T00:00:00Z", "author": self.target["author"]}
        self.replies.append(reply)
        observations, _ = self.collector.prepare({**self.spec, "kind": "discussion_replies", "node_id": "C1"}, [reply])
        value = make_event("unknown-bot-history", 1, None, **observations[0][2])
        history = value["conversation"]["history"]
        self.assertFalse(history["complete"])
        self.assertEqual([r["id"] for r in history["rows"]], ["D1", "C1", "R1"])
        self.assertNotIn("_actor_id", history["rows"][1])
        self.assertEqual(history["rows"][1], self.parent)
        self.assertFalse({"my_thread", "reply_to_me"} & set(value["facts"]))
        self.assertIn({"scope": "history", "code": "history_actor_unavailable"}, value["message"]["coverage_gaps"])
        unknown = self.provider.actor(self.parent)
        self.assertEqual(unknown["attribution"], "unknown")
        self.assertNotIn("platform_actor_id", unknown)
        self.assertNotIn("is_bot", unknown)

    def test_bare_Bot_discussion_lookup_failure_recovers_without_repost(self):
        self.prepare_discussion()
        key = ("GET", "/users/fixture-app%5Bbot%5D")
        self.server.overrides[key] = lambda *_: ((404, {}, {}) if self.posts else (200, self.server.bot, {}))
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:discussion-intent")["state"], "uncertain")
        self.assertIsNone(self.outbox.inspect("tool:discussion-intent")["parts"][0]["receipt"])
        self.advance()
        self.assertEqual(len(self.posts), 1)
        self.store.close()
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.outbox = Outbox(self.store, self.config)
        self.auth, self.http = self.client()
        from gh_presence.provider import GitHub
        self.provider = GitHub(self.http, self.store, config=self.config)
        self.sender = Sender(self.store, self.provider, FakeHost(), self.config)
        self.server.overrides.pop(key)
        self.sender.reconcile_uncertain()
        result = self.outbox.inspect("tool:discussion-intent")
        self.assertEqual(result["state"], "delivered")
        self.assertEqual(result["parts"][0]["receipt"]["author_id"], "901")
        self.assertEqual(len(self.posts), 1)

    def test_bare_Bot_discussion_readback_rejects_different_numeric_bot(self):
        self.prepare_discussion()
        self.server.overrides["GET", "/users/fixture-app%5Bbot%5D"] = lambda *_: (
            200, {**self.server.bot, "id": 902} if self.posts else self.server.bot, {})
        self.advance()
        result = self.outbox.inspect("tool:discussion-intent")
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(result["detail"]["code"], "readback_author_mismatch")
        self.assertIsNone(result["parts"][0]["receipt"])
        self.advance()
        self.assertEqual(len(self.posts), 1)

    def test_watched_bot_lookup_rejects_wrong_numeric_identity_type_and_login(self):
        selected = {"id": 901, "login": "fixture-app[bot]"}
        self.provider.identity(901)
        for changes in ({"id": 902}, {"type": "User"}, {"login": "other-app[bot]"}):
            with self.subTest(changes=changes):
                self.server.overrides["GET", "/users/fixture-app%5Bbot%5D"] = (200, {**self.server.bot, **changes}, {})
                with self.assertRaisesRegex(TransportError, "watched_identity_mismatch"):
                    self.provider.watched_identity(selected)

    def test_real_App_HTTP_outbox_comment_and_closure_keep_numeric_bot_receipt(self):
        self.prepare("close")
        self.advance()
        self.advance()
        result = self.outbox.inspect("tool:app-intent")
        self.assertEqual(result["state"], "delivered")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.closures), 1)
        receipt = result["parts"][0]["receipt"]
        self.assertEqual(str(receipt["author_id"]), "901")
        intent = json.loads(self.store.one("SELECT intent FROM outbox WHERE id='tool:app-intent'")["intent"])
        self.assertIn(".app.77.installation.88", intent["account_id"])
        self.assertFalse(any(c[1] == "/user" for c in self.server.calls))

    def test_unknown_readback_restart_and_refresh_never_reposts_or_closes(self):
        self.prepare("close")
        key = ("GET", "/repos/example/project/issues/comments/701")
        self.server.overrides[key] = (401, {}, {})
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:app-intent")["state"], "uncertain")
        self.assertEqual(len(self.posts), 1)
        self.store.close()
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.outbox = Outbox(self.store, self.config)
        self.auth, self.http = self.client()
        from gh_presence.provider import GitHub
        self.provider = GitHub(self.http, self.store, config=self.config)
        self.sender = Sender(self.store, self.provider, FakeHost(), self.config)
        self.advance()
        self.server.overrides[key] = lambda *_: (200, self.comments[0], {})
        self.sender.reconcile_uncertain()
        self.assertEqual(self.outbox.inspect("tool:app-intent")["parts"][0]["state"], "delivered")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.closures, [])

    def test_definite_401_post_is_not_replayed_after_renewal(self):
        self.prepare()
        key = ("POST", "/repos/example/project/issues/1/comments")
        self.server.overrides[key] = (401, {}, {})
        self.advance()
        self.assertEqual(self.outbox.inspect("tool:app-intent")["state"], "failed")
        self.provider.identity(901)
        self.advance()
        self.assertEqual(sum((r[0], r[1]) == key for r in self.server.calls), 1)
        self.assertEqual(len(self.server.mints), 2)

    def test_read_only_renewed_permission_holds_closure(self):
        self.prepare("close")
        self.advance()
        self.server.permissions["issues"] = "read"
        self.clock.advance(3601)
        self.advance()
        result = self.outbox.inspect("tool:app-intent")
        self.assertEqual(result["state"], "held")
        self.assertEqual(result["detail"]["code"], "closure_permission_not_proven")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.closures, [])

    def test_installation_change_preserves_old_custody_and_refuses_intent(self):
        self.prepare()
        other = Outbox(self.store, {**self.config, "installation_id": 89})
        with self.assertRaisesRegex(ValueError, "writer_account_changed"):
            other.tool(account_id=connection_account(self.config), conversation_id="repo.321", thread_id="issue.1",
                       source_event_id="source-app", body="Synthetic explanation", request_id="other")
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("app_companion", Path(__file__).resolve().parents[1] / "scripts/companion.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaisesRegex(TransportError, "state_namespace_changed"):
            module.build(self.store, {**self.config, "installation_id": 89}, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
