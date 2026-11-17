"""Chronological thread snapshots and bounded rereads of decisive reads (synthetic data only)."""
import json
import unittest
from types import SimpleNamespace

from test_attention_repair import (AttentionCase, OTHER, comment, gql, issue, pull)
from test_transport import CONFIG, HUMAN, REPO, WRITER
from gh_presence.collector import ENRICH_RECORD_ATTEMPTS, Collector
from gh_presence.events import thread_history
from gh_presence.host import Host
from gh_presence.http import Response, TransportError
from gh_presence.store import Store


def unavailable(status=503):
    return TransportError("provider_http_" + str(status), Response(status, {}, {}))


def pr_thread(size=5):
    """A pull request thread in GitHub's read order: general comments, then inline comments, then reviews."""
    return [pull(),
            {"id": 900, "node_id": "IC_900", "body": "g" * size, "user": OTHER, "created_at": "2026-02-05T00:00:00Z",
             "issue_url": "https://api.github.com/repos/example/project/issues/2"},
            {"id": 800, "node_id": "PRRC_800", "body": "i" * size, "user": HUMAN, "created_at": "2026-02-03T00:00:00Z",
             "pull_request_url": "https://api.github.com/repos/example/project/pulls/2", "pull_request_review_id": 700,
             "diff_hunk": "@@ -1 +1 @@", "path": "a.py"},
            {"id": 700, "node_id": "PRR_700", "body": "r" * size, "user": WRITER, "state": "COMMENTED",
             "submitted_at": "2026-02-02T00:00:00Z"}]


class ThreadOrderTests(AttentionCase):
    def test_the_snapshot_is_chronological_whatever_the_provider_read_order(self):
        snapshot = thread_history(pr_thread(), None, 11)
        self.assertEqual([m["message_id"] for m in snapshot["data"]["messages"]], ["PR_201", "PRR_700", "PRRC_800", "IC_900"])
        self.assertEqual([m["sent_at"][:10] for m in snapshot["data"]["messages"]],
                         ["2026-02-01", "2026-02-02", "2026-02-03", "2026-02-05"])

    def test_the_bound_keeps_the_newest_words_not_the_last_message_type_read(self):
        snapshot = thread_history(pr_thread(size=20000), None, 11)
        self.assertEqual([m["message_id"] for m in snapshot["data"]["messages"]], ["PR_201", "IC_900"])
        self.assertFalse(snapshot["data"]["complete"])

    def test_an_unknown_time_stays_after_the_known_ones_and_equal_times_keep_read_order(self):
        rows = pr_thread()
        rows[3] = {**rows[3], "submitted_at": None, "state": "PENDING"}
        rows[2] = {**rows[2], "created_at": "2026-02-05T00:00:00Z"}
        snapshot = thread_history(rows, None, 11)
        self.assertEqual([m["message_id"] for m in snapshot["data"]["messages"]], ["PR_201", "IC_900", "PRRC_800", "PRR_700"])

    def test_a_collected_reply_carries_the_chronological_thread_and_is_marked_last(self):
        rows = pr_thread()
        newest = {"id": 950, "node_id": "IC_950", "body": "A new question", "user": OTHER, "created_at": "2026-02-06T00:00:00Z",
                  "issue_url": "https://api.github.com/repos/example/project/issues/2"}
        self.provider.rows = rows[:2] + [newest] + rows[2:]  # general comments are read before inline comments and reviews
        value = self.page("comments", [newest])
        messages = value["conversation"]["thread_history"]["data"]["messages"]
        self.assertEqual([m["message_id"] for m in messages], ["PR_201", "PRR_700", "PRRC_800", "IC_900", "IC_950"])
        self.assertEqual(value["message"]["message_id"], messages[-1]["message_id"])


class RereadCase(AttentionCase):
    def run_enrichment(self, stream="repo:321:enrich"):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0 WHERE stream=?", (stream,))
        return self.tick_only(stream)

    def head(self, author):
        head = {"id": "D_7", "databaseId": 507, "number": 7, "title": "Idea", "body": "Let us discuss",
                "createdAt": "2026-02-01T00:00:00Z", "updatedAt": "2026-02-01T00:00:00Z", "author": author}
        self.provider.graph_pages[("discussions", None, None)] = {"nodes": [head], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        with self.store.transaction() as db:
            self.store.job(db, "repo:321:fixture-discussions", {"kind": "discussions", "repo": REPO})
        self.assertTrue(self.tick_only("repo:321:fixture-discussions"))
        return head

    def reply(self, ident, author, created="2026-02-02T00:00:00Z", body="A reply"):
        return {"id": ident, "databaseId": int(ident.split("_")[1]), "body": body, "createdAt": created, "updatedAt": created,
                "replyTo": None, "author": author}

    def comment_page(self, row):
        self.provider.graph_pages[("discussion_comments", "D_7", None)] = {"nodes": [row], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        with self.store.transaction() as db:
            db.execute("DELETE FROM jobs WHERE stream='repo:321:fixture-comments'")
            self.store.job(db, "repo:321:fixture-comments", {"kind": "discussion_comments", "repo": REPO, "number": 7,
                                                              "discussion_id": "D_7", "node_id": "D_7"})
        self.assertTrue(self.tick_only("repo:321:fixture-comments"))
        return self.latest()


class AuthorRereadTests(RereadCase):
    def test_a_transient_author_lookup_is_retried_before_the_first_offer(self):
        head = self.head(gql(WRITER))  # its watched opener proves my_thread: no history wait
        row = self.reply("DC_601", {"login": "visitor", "__typename": "User"})
        self.provider.rows = [head, row]
        self.provider.pages["/users/visitor"] = unavailable()
        waiting = self.comment_page(row)
        self.assertEqual(waiting["_state"], "enriching")
        self.assertNotIn("platform_actor_id", waiting["actor"])
        self.provider.pages["/users/visitor"] = Response(200, {"id": 33, "login": "visitor", "type": "User"}, {})
        self.assertTrue(self.run_enrichment())
        done = self.latest()
        self.assertEqual((done["_state"], done["source_event_id"]), ("ready", waiting["source_event_id"]))
        self.assertEqual(done["actor"]["platform_actor_id"], "33")
        self.assertIn("my_thread", done["facts"])
        self.assertEqual(done["message"]["raw"], row)

    def test_a_definite_author_lookup_failure_does_not_wait(self):
        head = self.head(gql(WRITER))
        row = self.reply("DC_602", {"login": "visitor", "__typename": "User"})
        self.provider.rows = [head, row]
        self.provider.pages["/users/visitor"] = TransportError("no_longer_visible", Response(404, {}, {}))
        value = self.comment_page(row)
        self.assertEqual(value["_state"], "ready")
        self.assertEqual(value["actor"]["attribution"], "unknown")

    def test_a_bot_author_stays_a_bot_while_and_after_its_number_is_retried(self):
        head = self.head(gql(WRITER))
        row = self.reply("DC_603", {"login": "helper-app", "__typename": "Bot"})
        self.provider.rows = [head, row]
        self.provider.pages["/users/helper-app%5Bbot%5D"] = unavailable()
        waiting = self.comment_page(row)
        self.assertEqual((waiting["_state"], waiting["actor"].get("is_bot")), ("enriching", True))
        self.assertIn("from_bot", waiting["facts"])
        runs = 0
        while self.latest()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, 10)
            self.run_enrichment()
        done = self.latest()
        self.assertEqual((done["actor"].get("is_bot"), done["actor"]["attribution"]), (True, "unknown"))
        self.assertIn("from_bot", done["facts"])
        self.assertIn({"scope": "actor", "code": "provider_http_503"}, done["message"]["coverage_gaps"])


class HistoryRereadTests(RereadCase):
    def test_a_watched_history_author_lookup_is_retried_and_an_unrelated_one_is_not(self):
        head = self.head(gql(OTHER))  # the opening does not prove my thread
        earlier = self.reply("DC_610", {"login": "service-writer", "__typename": "User"}, created="2026-02-01T12:00:00Z")
        stranger = self.reply("DC_611", {"login": "stranger", "__typename": "User"}, created="2026-02-01T13:00:00Z")
        new = self.reply("DC_612", gql(OTHER), body="Any news?")
        self.provider.rows = [head, earlier, stranger, new]
        self.provider.pages["/users/service-writer"] = unavailable()
        self.provider.pages["/users/stranger"] = unavailable()
        waiting = self.comment_page(new)
        self.assertEqual(waiting["_state"], "enriching")
        self.provider.pages["/users/service-writer"] = Response(200, {"id": 11, "login": "service-writer", "type": "User"}, {})
        self.assertTrue(self.run_enrichment())
        done = self.latest()
        self.assertEqual(done["_state"], "ready")
        self.assertIn("my_thread", done["facts"])
        self.assertIn({"scope": "history", "code": "history_actor_unavailable"}, done["message"]["coverage_gaps"])  # the stranger
        # Only an unrelated author unresolved: the new words are not held.
        self.provider.pages["/users/service-writer"] = Response(200, {"id": 11, "login": "service-writer", "type": "User"}, {})
        other = self.reply("DC_613", gql(OTHER), body="Still here", created="2026-02-02T01:00:00Z")
        self.provider.rows = [head, earlier, stranger, new, other]
        self.provider.users.clear()
        self.assertEqual(self.comment_page(other)["_state"], "ready")


class PartialRecoveryTests(RereadCase):
    """Two decisive reads of one event that recover in different attempts: what one attempt established stays with
    the waiting event, through a restart, when a later attempt's read of the same thing fails."""

    def restart(self):
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.outbox.store = self.store
        self.provider.users.clear()  # a new process starts without its lookup cache
        self.collector = Collector(self.store, self.provider, CONFIG)

    def test_history_read_on_one_attempt_survives_a_restart_and_its_own_later_failures(self):
        head = self.head(gql(OTHER))  # the opening does not prove my thread
        earlier = self.reply("DC_620", gql(WRITER), created="2026-02-01T12:00:00Z")
        new = self.reply("DC_621", {"login": "visitor", "__typename": "User"}, body="Any news?")
        self.provider.rows = [head, earlier, new]
        self.provider.pages["/users/visitor"] = unavailable()
        self.provider.snapshot_errors = [TransportError("read_unavailable")]
        waiting = self.comment_page(new)
        event_id = waiting["source_event_id"]
        self.assertEqual(waiting["_state"], "enriching")
        self.assertEqual(sorted(n["need"] for n in self.store.meta("enrich:" + event_id)["needs"]), ["author", "history"])
        self.assertNotIn("my_thread", waiting["facts"])
        # Attempt 2: the thread is read and proves my thread; the author lookup still fails.
        self.assertFalse(self.run_enrichment())
        saved = self.store.meta("enrich:" + event_id)
        self.assertEqual([n["need"] for n in saved["needs"]], ["author"])  # only what is still open is tried again
        self.assertEqual([row["id"] for row in saved["kwargs"]["history"]], ["D_7", "DC_620", "DC_621"])
        read_at = saved["kwargs"]["history_at"]
        self.restart()
        # Attempt 3: now the thread read fails for good, and the author lookup answers.
        self.provider.snapshot_errors = [TransportError("read_unavailable")] * 10
        self.provider.pages["/users/visitor"] = Response(200, {"id": 33, "login": "visitor", "type": "User"}, {})
        self.assertTrue(self.run_enrichment())
        done = self.latest()
        self.assertEqual((done["_state"], done["source_event_id"]), ("ready", event_id))
        self.assertEqual(done["actor"]["platform_actor_id"], "33")
        self.assertIn("my_thread", done["facts"])  # proven on attempt 2, never taken back
        self.assertEqual(done["message"]["fact_evidence"]["my_thread"][0]["actor_id"], "11")
        self.assertEqual(len(self.provider.snapshot_errors), 10)  # the thread was not read again
        self.assertEqual(done["conversation"]["history"]["observed_at"], read_at)  # when it really was read
        self.assertEqual(done["message"]["observed_at"], waiting["message"]["observed_at"])
        self.assertEqual(done["message"]["raw"], new)
        self.assertIsNone(self.store.meta("enrich:" + event_id))

    def test_a_history_author_resolved_before_a_restart_stays_resolved(self):
        head = self.head(gql(OTHER))
        mine = self.reply("DC_630", {"login": "service-writer", "__typename": "User"}, created="2026-02-01T12:00:00Z")
        other = self.reply("DC_631", {"login": "contributor", "__typename": "User"}, created="2026-02-01T13:00:00Z")
        new = self.reply("DC_632", gql(OTHER), body="Any news?")
        self.provider.rows = [head, mine, other, new]
        self.provider.pages["/users/service-writer"] = Response(200, {"id": 11, "login": "service-writer", "type": "User"}, {})
        self.provider.pages["/users/contributor"] = unavailable()
        waiting = self.comment_page(new)
        self.assertEqual(waiting["_state"], "enriching")  # another watched author is still unknown
        self.restart()
        self.provider.pages["/users/service-writer"] = unavailable()  # after the restart neither lookup answers
        runs = 0
        while self.latest()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, 10)
            self.run_enrichment()
        done = self.latest()
        self.assertIn("my_thread", done["facts"])
        self.assertEqual(done["message"]["fact_evidence"]["my_thread"][0]["actor_id"], "11")
        self.assertIn({"scope": "history", "code": "history_actor_unavailable"}, done["message"]["coverage_gaps"])
        row = next(r for r in done["conversation"]["history"]["rows"] if r["id"] == "DC_630")
        self.assertEqual(row["_actor_id"], "11")

    def test_a_change_record_that_answered_stays_when_later_reads_fail(self):
        self.capable()
        self.provider.rows = [issue()]
        self.page("issues", [issue()])
        changed = issue(state="closed", body="Rewritten", updated_at="2026-02-02T00:00:00Z")
        self.provider.rows = [changed]
        # The first read fails; the second answers with the edit's record but not yet the close; then GitHub fails.
        answered = {"edits": [{"edited_at": changed["updated_at"], "editor": gql(OTHER)}], "events": []}
        self.provider.records[("thread", 1)] = [unavailable(), answered] + [unavailable()] * 10
        waiting = self.page("issues", [changed])
        self.assertEqual(waiting["_state"], "enriching")
        runs = 0
        while self.latest()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, 10)
            self.run_enrichment()
        done = self.latest()
        self.assertEqual(done["actor"]["attribution"], "unknown")
        # The record that did answer names who edited the text: kept, with its own gap, not a bare read failure.
        self.assertIn({"scope": "actor", "code": "change_record_missing"}, done["message"]["coverage_gaps"])
        self.assertEqual(done["message"]["fact_evidence"]["change_actor"]["records"],
                         [{"change": "body", "source": "UserContentEdit", "at": changed["updated_at"], "actor_id": "33",
                           "login": "visitor"}])


class ChangeRereadTests(AttentionCase):
    def run_enrichment(self):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0 WHERE stream='repo:321:enrich'")
        return self.tick_only("repo:321:enrich")

    def observed(self, raw, kind="issues"):
        self.provider.rows = [raw]
        return self.page(kind, [raw])

    def test_a_transient_change_actor_lookup_is_retried(self):
        self.capable()
        self.observed(issue())
        edited = issue(body="Edited", updated_at="2026-02-02T00:00:00Z")
        self.provider.records[("thread", 1)] = {"edits": [{"edited_at": edited["updated_at"], "editor": {"login": "visitor", "__typename": "User"}}]}
        self.provider.pages["/users/visitor"] = unavailable()
        waiting = self.observed(edited)
        self.assertEqual(waiting["_state"], "enriching")
        self.provider.pages["/users/visitor"] = Response(200, {"id": 33, "login": "visitor", "type": "User"}, {})
        self.assertTrue(self.run_enrichment())
        done = self.latest()
        self.assertEqual((done["_state"], done["actor"]["platform_actor_id"]), ("ready", "33"))

    def test_a_delayed_change_record_is_reread_a_bounded_number_of_times(self):
        self.capable()
        self.observed(issue())
        closed = issue(state="closed", updated_at="2026-02-02T00:00:00Z")
        record = {"events": [{"type": "ClosedEvent", "created_at": closed["updated_at"], "actor": gql(OTHER)}]}
        self.provider.records[("thread", 1)] = [{"events": []}, record]
        waiting = self.observed(closed)
        self.assertEqual(waiting["_state"], "enriching")
        self.assertTrue(self.run_enrichment())
        done = self.latest()
        self.assertEqual((done["_state"], done["actor"]["platform_actor_id"]), ("ready", "33"))
        # A record that never appears: a few rereads, then the change goes out unattributed with its gap.
        reopened = issue(state="open", updated_at="2026-02-03T00:00:00Z")
        self.provider.records[("thread", 1)] = {"events": []}
        self.assertEqual(self.observed(reopened)["_state"], "enriching")
        runs = 0
        while self.latest()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_RECORD_ATTEMPTS)
            self.run_enrichment()
        self.assertEqual(runs, ENRICH_RECORD_ATTEMPTS - 1)
        final = self.latest()
        self.assertEqual(final["actor"]["attribution"], "unknown")
        self.assertIn({"scope": "actor", "code": "change_record_missing"}, final["message"]["coverage_gaps"])

    def test_a_change_github_keeps_no_record_for_is_not_reread(self):
        self.capable()
        inline = {"id": 820, "node_id": "PRRC_820", "body": "Nit", "user": OTHER, "line": 10, "path": "a.py",
                  "created_at": "2026-02-02T00:00:00Z", "updated_at": "2026-02-02T00:00:00Z", "diff_hunk": "@@",
                  "pull_request_review_id": 700, "pull_request_url": "https://api.github.com/repos/example/project/pulls/2"}
        self.provider.rows = [pull(), inline]
        self.page("inline", [inline])
        moved = {**inline, "line": None}
        self.provider.rows = [pull(), moved]
        value = self.page("inline", [moved])
        self.assertEqual(value["_state"], "ready")
        self.assertIn({"scope": "actor", "code": "change_record_missing"}, value["message"]["coverage_gaps"])

    def test_a_waiting_change_holds_later_changes_but_never_new_words(self):
        self.capable()
        opened = self.observed(issue())  # earlier than the waiting change: it goes out in any case
        self.provider.records[("thread", 1)] = {"events": []}
        waiting = self.observed(issue(state="closed", updated_at="2026-02-02T00:00:00Z"))
        self.assertEqual(waiting["_state"], "enriching")
        self.provider.records[("thread", 1)] = {"events": [{"type": "LockedEvent", "created_at": "2026-02-02T00:05:00Z", "actor": gql(OTHER)}]}
        later_change = self.observed(issue(state="closed", locked=True, updated_at="2026-02-02T00:05:00Z"))
        self.assertEqual((later_change["_state"], later_change["actor"]["platform_actor_id"]), ("ready", "33"))
        reply = comment(640, OTHER, "Why was this closed?", created="2026-02-02T00:06:00Z")
        self.provider.rows = [issue(state="closed", locked=True), reply]
        new_words = self.page("comments", [reply])
        self.assertEqual(new_words["_state"], "ready")
        sent = []
        host = Host(SimpleNamespace(request=lambda method, path, payload: sent.append(payload["event"]["source_event_id"])
                                    or Response(200, {"status": "completed", "outcome": "silent"}, {})), self.store, self.outbox)
        host.dispatch()
        host.close()
        # New words go out at once; the later change keeps its place behind the change still being read.
        self.assertEqual(sent, [opened["source_event_id"], new_words["source_event_id"]])
        self.assertEqual(self.store.one("SELECT state FROM inbox WHERE id=?", (later_change["source_event_id"],))["state"], "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
