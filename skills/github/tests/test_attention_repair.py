"""0.2.0 attention repair: who made a change, words versus state, documented context, retried decisive reads.

Synthetic accounts and repositories only; no provider network and no host process.
"""
import copy
import json
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from test_transport import CONFIG, HOST_IDENTITY, HUMAN, REPO, WATCHED, WRITER, Case, FakeProvider, Loopback
from gh_presence.collector import ENRICH_ATTEMPTS, Collector
from gh_presence.events import (SIGNAL_NEW_DISCUSSION, THREAD_CHAR_BOUND, attribute, comparison, make_event,
                                observed_changes, state_changes, thread_history)
from gh_presence.host import Host, wire_event
from gh_presence.http import HTTP, Response, TransportError
from gh_presence.provider import GitHub, NODE_CHANGES, THREAD_CHANGES
from gh_presence.store import Store, digest, dump

OTHER = {"id": 33, "login": "visitor", "type": "User"}
ROOT = Path(__file__).resolve().parents[1]


def gql(user, kind="User"):
    return {"login": user["login"], "__typename": kind, "databaseId": user["id"]}


def target(user, kind="User"):
    return {"typename": kind, "id": user["id"], "login": user["login"]}


def issue(**changes):
    value = {"id": 101, "node_id": "I_101", "number": 1, "title": "Question", "body": "Source text", "user": HUMAN,
             "state": "open", "locked": False, "assignees": [], "created_at": "2026-02-01T00:00:00Z",
             "updated_at": "2026-02-01T00:00:00Z", "html_url": "https://github.com/example/project/issues/1"}
    return {**value, **changes}


def pull(**changes):
    value = {"id": 201, "node_id": "PR_201", "number": 2, "title": "Fix", "body": "Original body", "user": HUMAN,
             "state": "open", "draft": False, "locked": False, "assignees": [], "pull_request": {"merged_at": None},
             "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z"}
    return {**value, **changes}


def comment(ident, user, body, created="2026-02-02T00:00:00Z", number=1, **changes):
    return {"id": ident, "node_id": "IC_" + str(ident), "body": body, "user": user, "created_at": created,
            "updated_at": created, "issue_url": "https://api.github.com/repos/example/project/issues/" + str(number), **changes}


class ChangeProvider(FakeProvider):
    """FakeProvider with GitHub's real actor resolution and a scripted change record."""
    actor = GitHub.actor
    actor_of = GitHub.actor_of

    def __init__(self):
        super().__init__()
        self.users, self.records, self.record_calls, self.snapshot_errors = {}, {}, [], []

    def thread_snapshot(self, route):
        if self.snapshot_errors:
            error = self.snapshot_errors.pop(0)
            if error is not None:
                raise error
        return super().thread_snapshot(route)

    def change_record(self, route, *, since=None, node=None, before=None):
        key = ("node", node) if node else ("thread", route["number"])
        self.record_calls.append((key, since) if before is None else (key, since, before))
        value = self.records.get(key, {"events": [], "edits": []})
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class AttentionCase(Case):
    def setUp(self):
        super().setUp()
        self.provider = ChangeProvider()
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET state='accepted' WHERE id='event-1'")
        self.store.set_meta("bootstrap:repo:321", "2026-01-01T00:00:00+00:00")
        self.collector = Collector(self.store, self.provider, CONFIG)
        self.pages = 0

    def capable(self, change=True, signals=True):
        self.store.set_meta("host_capabilities", {"ready": True, **({"presence_change_context": 1} if change else {}),
                                                  **({"presence_wake_signals": 1} if signals else {})})

    def tick_only(self, stream):
        """The real tick() on exactly one stream; every other pending job is parked meanwhile."""
        others = [r["id"] for r in self.store.rows("SELECT id FROM jobs WHERE state='pending' AND stream!=?", (stream,))]
        with self.store.transaction() as db:
            db.executemany("UPDATE jobs SET state='parked' WHERE id=?", [(i,) for i in others])
            db.execute("UPDATE jobs SET due=0 WHERE stream=? AND state='pending'", (stream,))
        try:
            return self.collector.tick()
        finally:
            with self.store.transaction() as db:
                db.executemany("UPDATE jobs SET state='pending' WHERE id=? AND state='parked'", [(i,) for i in others])

    def page(self, kind, rows, **extra):
        self.pages += 1
        path = "/fixture/" + kind + "/" + str(self.pages)
        self.provider.pages[path] = Response(200, copy.deepcopy(rows), {})
        stream = "repo:321:fixture-" + kind
        with self.store.transaction() as db:
            self.store.job(db, stream, {"kind": kind, "repo": REPO, "path": path, **extra})
        self.assertTrue(self.tick_only(stream))
        return self.latest()

    def latest(self):
        row = self.store.one("SELECT id,state,event FROM inbox WHERE id!='event-1' ORDER BY seq DESC LIMIT 1")
        return dict(json.loads(row["event"]), _state=row["state"]) if row else None

    def events(self):
        return [dict(json.loads(r["event"]), _state=r["state"])
                for r in self.store.rows("SELECT state,event FROM inbox WHERE id!='event-1' ORDER BY seq")]

    def non_text(self, value, kind="state_change"):
        return [item["description"] for item in value["message"].get("non_text", []) if item["kind"] == kind]


class CapabilityTests(AttentionCase):
    def test_change_observations_need_the_exact_integer_capability(self):
        for advertised, expected in ((1, True), ("1", False), (True, False), (2, False), (None, False)):
            with self.subTest(advertised=advertised):
                value = {**HOST_IDENTITY, **({} if advertised is None else {"presence_change_context": advertised})}
                host = Host(SimpleNamespace(request=lambda *a, v=value: Response(200, v, {})), self.store, self.outbox)
                self.addCleanup(host.close)
                host.negotiate()
                self.assertEqual(self.collector.capabilities()["change"], expected)

    def test_manifest_declares_the_discussion_signal_beside_core_facts(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        manifest = yaml.safe_load(text.split("---")[1])
        place, = manifest["contributes"]
        self.assertIn({"id": SIGNAL_NEW_DISCUSSION, "label": "New discussion"}, place["facts"])
        declared = {f if isinstance(f, str) else f["id"] for f in place["facts"]}
        self.assertTrue(set(place["wakes"]) <= declared)
        self.assertNotIn(SIGNAL_NEW_DISCUSSION, place["wakes"])  # an opt-in signal, as the hub's other connectors
        self.assertTrue({"any_message", "from_bot", "reply_to_me", "assigned", "review_requested"} <= declared)


class NewWordsTests(AttentionCase):
    def test_new_words_carry_message_id_identity_mentions_and_the_documented_thread(self):
        for change in (True, False):
            with self.subTest(change_context=change):
                self.capable(change=change)
                opening = issue(user=OTHER)
                earlier = comment(500, WRITER, "Earlier answer", created="2026-02-01T01:00:00Z")
                self.provider.rows = [opening, earlier]
                new = comment(600 + change, OTHER, "Thanks @service-writer and @contributor, `@ignored`")
                value = self.page("comments", [new])
                message = value["message"]
                self.assertEqual(value["text"], new["body"])
                self.assertEqual(value["actor"]["platform_actor_id"], "33")
                self.assertEqual(message["message_id"], new["node_id"])
                self.assertEqual(message["mentions"], [{"id": "11", "name": "service-writer", "is_self": True},
                                                       {"id": "22", "name": "contributor", "is_self": False}])
                self.assertEqual([e["user_id"] for e in message["fact_evidence"]["mention"]], [11, 22])  # parser evidence kept apart
                self.assertTrue({"any_message", "mention", "my_thread"} <= set(value["facts"]))
                thread = value["conversation"]["thread_history"]
                self.assertEqual(thread["status"], "available")
                messages = thread["data"]["messages"]
                self.assertEqual([m["message_id"] for m in messages], ["I_101", "IC_500"])
                self.assertEqual(messages[1], {"message_id": "IC_500", "kind": "issue_comment", "text": "Earlier answer",
                                               "sent_at": "2026-02-01T01:00:00+00:00", "platform_actor_id": "11",
                                               "username": "service-writer"})
                self.assertEqual(messages[0]["text"], "Question\n\nSource text")
                self.assertIn({"id": "11", "name": "service-writer", "is_self": True}, thread["authors"])
                self.assertNotIn("user", messages[0])  # the host reads a flat author id, never a provider mapping
                self.assertNotIn("non_text", message)
                wire = wire_event(value)
                self.assertNotIn("rows", wire["conversation"]["history"])
                self.assertEqual(wire["conversation"]["history"]["retained_rows"], 2)

    def test_a_new_comment_its_author_alone_edited_before_first_sight_stays_new_words(self):
        self.capable()
        self.provider.rows = [issue()]
        new = comment(610, OTHER, "Edited before the first poll", updated_at="2026-02-02T05:00:00Z")
        # GitHub's own edit record of the comment: its original revision and one edit, both by its author.
        self.provider.records[("node", "IC_610")] = {"edits": [
            {"edited_at": "2026-02-02T05:00:00Z", "deleted": False, "editor": gql(OTHER)},
            {"edited_at": "2026-02-02T00:00:00Z", "deleted": False, "editor": gql(OTHER)}]}
        value = self.page("comments", [new])
        self.assertEqual((value["_state"], value["message"]["action"]), ("ready", "observed"))
        self.assertEqual(value["actor"]["platform_actor_id"], "33")
        self.assertIn("any_message", value["facts"])
        self.assertNotIn("edited_at", value["message"])
        self.assertNotIn("forward_origin", value["message"])
        self.assertFalse(self.non_text(value))
        self.assertEqual(self.provider.record_calls, [(("node", "IC_610"), None)])  # one read of that record

    def test_thread_snapshot_is_bounded_but_keeps_its_opening_and_the_waking_message(self):
        rows = [issue(body="opening")] + [comment(700 + n, OTHER, "x" * 4000, created="2026-02-02T00:00:%02dZ" % n) for n in range(20)]
        rows[3]["body"] = "the edited one"
        snapshot = thread_history(rows, None, 11, waking=rows[3]["node_id"])
        ids = [m["message_id"] for m in snapshot["data"]["messages"]]
        self.assertEqual(ids[0], "I_101")
        self.assertIn(rows[3]["node_id"], ids)
        self.assertEqual(ids[-1], rows[-1]["node_id"])
        self.assertFalse(snapshot["data"]["complete"])
        self.assertLessEqual(sum(len(m["text"]) for m in snapshot["data"]["messages"]), THREAD_CHAR_BOUND + 4000 + 100)
        self.assertIn("github_place_status", snapshot["note"])


class ChangeTests(AttentionCase):
    def first(self, raw, kind="issues"):
        self.provider.rows = [raw]
        return self.page(kind, [raw])

    def test_state_change_has_no_words_and_names_the_account_that_made_it(self):
        self.capable()
        opened = self.first(issue())
        self.assertEqual(opened["facts"][:2], ["any_message", "new_issue"])  # its watched opener also makes it my_thread
        self.assertEqual(opened["actor"]["platform_actor_id"], "22")
        for actor, expected in ((WRITER, "11"), (OTHER, "33")):
            with self.subTest(actor=actor["login"]):
                closed = issue(state="open" if expected == "33" else "closed", state_reason=None if expected == "33" else "completed",
                               updated_at="2026-02-0%dT00:00:05Z" % (2 if expected == "11" else 3))
                kind = "ClosedEvent" if expected == "11" else "ReopenedEvent"
                self.provider.records[("thread", 1)] = {"events": [{"type": kind, "created_at": closed["updated_at"], "actor": gql(actor)}]}
                self.provider.rows = [closed]
                value = self.page("issues", [closed])
                self.assertEqual(value["text"], "")
                self.assertEqual(value["facts"], [])  # never any_message merely because the object changed
                self.assertEqual(value["actor"]["platform_actor_id"], expected)
                self.assertEqual(value["message"]["original_author"]["platform_actor_id"], "22")
                self.assertEqual(self.non_text(value), ["Closed the issue as completed."] if expected == "11" else ["Reopened the issue."])
                self.assertEqual(self.non_text(value, "change_attribution"),
                                 ["GitHub records @" + actor["login"] + " (id " + expected + ") as having made this change."])
                self.assertNotIn("edited_at", value["message"])
                self.assertNotIn("forward_origin", value["message"])
                self.assertEqual(value["message"]["raw"], closed)
                self.assertEqual(value["message"]["fact_evidence"]["change_actor"]["status"], "attributed")
        self.assertEqual(self.provider.record_calls[0][1], "2026-01-31T23:59:00+00:00")

    def test_own_and_human_assignments_differ_only_by_their_recorded_actor(self):
        self.capable()
        self.first(issue())
        cases = ((WRITER, [HUMAN], "11"), (OTHER, [HUMAN, WRITER], "33"))
        for n, (actor, assignees, expected) in enumerate(cases):
            changed = issue(assignees=assignees, updated_at="2026-02-0%dT00:00:00Z" % (2 + n))
            added = assignees[-1]
            self.provider.records[("thread", 1)] = {"events": [
                {"type": "AssignedEvent", "created_at": changed["updated_at"], "actor": gql(actor), "assignee": target(added)}]}
            value = self.first(changed)
            self.assertEqual(value["facts"], ["assigned"])
            self.assertEqual(value["actor"]["platform_actor_id"], expected)
            self.assertEqual(self.non_text(value), ["Assigned @" + added["login"] + " (id " + str(added["id"]) + ")."])

    def test_text_revision_keeps_revised_words_editor_author_and_edit_time(self):
        self.capable()
        self.first(pull())
        # Its author's edits, then a maintainer's, then its author's again: GitHub's edit record grows with each.
        cases = ((HUMAN, "Original body, fixed by its author", "own"), (HUMAN, "Now asking @service-writer", "own"),
                 (WRITER, "Now asking @service-writer\n\nAdded by a maintainer", "foreign"),
                 (HUMAN, "Rewritten again by its author", "mixed"))
        edits = [{"edited_at": "2026-02-01T00:00:00Z", "deleted": False, "editor": gql(HUMAN)}]
        for n, (editor, body, origin) in enumerate(cases):
            with self.subTest(editor=editor["login"], body=body):
                edited = pull(body=body, updated_at="2026-02-0%dT00:00:10Z" % (2 + n))
                edits.insert(0, {"edited_at": edited["updated_at"], "deleted": False, "editor": gql(editor)})
                self.provider.records[("thread", 2)] = {"edits": list(edits), "events": []}
                value = self.first(edited)
                message = value["message"]
                self.assertEqual(value["text"], "Fix\n\n" + body)
                self.assertEqual(value["actor"]["platform_actor_id"], str(editor["id"]))
                self.assertEqual(message["original_author"]["platform_actor_id"], "22")
                self.assertEqual(message["edited_at"], edited["updated_at"].replace("Z", "+00:00"))
                self.assertEqual(self.non_text(value), ["Edited the text of the pull request."])
                self.assertEqual(value["facts"], ["mention"] if n == 1 else [])
                if origin == "foreign":
                    self.assertEqual(message["forward_origin"], {"sender_user": {"id": "22", "username": "contributor",
                                                                                 "display_name": "contributor"}})
                    self.assertTrue(self.non_text(value, "change_attribution")[0].startswith(
                        "Edited by @service-writer (id 11); the text was first written by @contributor (id 22)."))
                elif origin == "mixed":
                    # Its first author edits again, but the maintainer's words are still in it: never theirs alone.
                    self.assertEqual(message["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
                    self.assertIn("not established as theirs alone", self.non_text(value, "change_attribution")[0])
                else:
                    self.assertNotIn("forward_origin", message)
                    self.assertEqual(self.non_text(value, "change_attribution"),
                                     ["GitHub records @contributor (id 22) as having made this change."])

    def test_several_actors_between_two_polls_leave_the_change_unattributed(self):
        self.capable()
        self.first(issue())
        changed = issue(body="Rewritten", assignees=[HUMAN], updated_at="2026-02-02T00:00:00Z")
        self.provider.records[("thread", 1)] = {
            "edits": [{"edited_at": "2026-02-01T12:00:00Z", "editor": gql(OTHER)}],
            "events": [{"type": "AssignedEvent", "created_at": "2026-02-02T00:00:00Z", "actor": gql(WRITER), "assignee": target(HUMAN)}]}
        value = self.first(changed)
        self.assertEqual(value["actor"], {"platform": "github", "attribution": "unknown"})
        self.assertIn({"scope": "actor", "code": "multiple_change_actors"}, value["message"]["coverage_gaps"])
        self.assertEqual(value["facts"], ["assigned"])
        self.assertIn("forward_origin", value["message"])  # whoever edited, the words are not the unknown actor's own
        self.assertIn("@service-writer", self.non_text(value, "change_attribution")[0])

    def test_a_change_without_its_own_record_stays_unknown(self):
        self.capable()
        self.first(issue())
        value = self.first(issue(state="closed", updated_at="2026-02-02T00:00:00Z"))
        self.assertEqual(value["actor"]["attribution"], "unknown")
        self.assertIn({"scope": "actor", "code": "change_record_missing"}, value["message"]["coverage_gaps"])
        stale = {"type": "ClosedEvent", "created_at": "2026-01-15T00:00:00Z", "actor": gql(WRITER)}
        self.provider.records[("thread", 1)] = {"events": [stale]}  # older than the previous snapshot: not this change
        value = self.first(issue(state="open", updated_at="2026-02-03T00:00:00Z"))
        self.assertEqual(value["actor"]["attribution"], "unknown")

    def test_bot_without_a_number_keeps_from_bot_and_an_unknown_editor_is_no_bot(self):
        self.capable()
        self.first(issue())
        for n, (editor, bot) in enumerate((({"login": "helper-app", "__typename": "Bot"}, True), (None, False))):
            edited = issue(body="Edit " + str(n), updated_at="2026-02-0%dT00:00:00Z" % (2 + n))
            self.provider.records[("thread", 1)] = {"edits": [{"edited_at": edited["updated_at"], "editor": editor}]}
            value = self.first(edited)
            self.assertNotIn("platform_actor_id", value["actor"])
            self.assertNotEqual(value["actor"].get("platform_actor_id"), "0")
            self.assertEqual("from_bot" in value["facts"], bot)
            self.assertEqual(value["actor"].get("is_bot"), True if bot else None)
            if bot:
                self.assertEqual(value["actor"]["username"], "helper-app[bot]")

    def test_first_sight_of_an_older_updated_object_is_a_change_with_its_provenance_gap(self):
        self.capable()
        raw = issue(created_at="2025-12-01T00:00:00Z", updated_at="2026-01-05T00:00:00Z", assignees=[WRITER],
                    body="Mentions @service-writer")
        value = self.first(raw)
        self.assertEqual(value["message"]["action"], "updated_before_first_observation")
        self.assertEqual(value["text"], "")
        self.assertEqual(value["facts"], [])
        self.assertEqual(value["actor"], {"platform": "github", "attribution": "unknown"})
        self.assertNotIn("edited_at", value["message"])
        self.assertIn({"scope": "revision", "code": "prior_revision_unavailable"}, value["message"]["coverage_gaps"])
        self.assertIn("first observed", self.non_text(value)[0])
        self.assertEqual(value["message"]["raw"]["body"], raw["body"])  # the observed words stay as source
        self.assertFalse(self.provider.record_calls)

    def test_a_host_without_the_capability_keeps_the_0_1_6_revision_form(self):
        self.first(issue())
        closed = self.first(issue(state="closed", updated_at="2026-02-02T00:00:00Z"))
        edited = self.first(issue(state="closed", body="Changed", updated_at="2026-02-03T00:00:00Z"))
        for value in (closed, edited):
            self.assertEqual(value["facts"], ["any_message"])
            self.assertEqual(value["actor"], {"platform": "github", "attribution": "unknown"})
            self.assertNotIn("non_text", value["message"])
            self.assertNotIn("forward_origin", value["message"])
        self.assertEqual(closed["text"], "Question\n\nSource text")
        self.assertEqual(edited["message"]["edited_at"], "2026-02-03T00:00:00+00:00")
        self.assertFalse(self.provider.record_calls)
        self.assertEqual(closed["message"]["message_id"], "I_101")  # shared improvements reach every host


class PullRequestTests(AttentionCase):
    def test_a_pull_request_is_one_semantic_object(self):
        self.capable()
        self.provider.rows = [pull()]
        self.page("issues", [pull()])
        kinds = [json.loads(r["spec"])["kind"] for r in self.store.rows("SELECT spec FROM jobs WHERE stream LIKE '%children'")]
        self.assertEqual(sorted(kinds), ["requests", "reviews"])
        before = len(self.events())
        with self.store.transaction() as db:  # a details page queued by an older version
            self.store.job(db, "repo:321:legacy:children", {"kind": "pull", "repo": REPO, "number": 2, "path": "/fixture/pull"})
        self.provider.pages["/fixture/pull"] = Response(200, {**pull(), "merged": False}, {})
        self.assertTrue(self.tick_only("repo:321:legacy:children"))
        self.assertEqual(len(self.events()), before)
        merged = pull(state="closed", pull_request={"merged_at": "2026-02-04T00:00:00Z"}, updated_at="2026-02-04T00:00:00Z")
        self.provider.records[("thread", 2)] = {"events": [{"type": "MergedEvent", "created_at": "2026-02-04T00:00:00Z", "actor": gql(WRITER)},
                                                           {"type": "ClosedEvent", "created_at": "2026-02-04T00:00:00Z", "actor": gql(WRITER)}]}
        self.provider.rows = [merged]
        value = self.page("issues", [merged])
        self.assertEqual(len(self.events()), before + 1)
        self.assertEqual(self.non_text(value), ["Merged and closed the pull request."])
        self.assertEqual(value["actor"]["platform_actor_id"], "11")

    def test_a_closed_pull_request_reopened_and_merged_between_two_polls_is_seen(self):
        self.capable()
        closed = pull(state="closed", updated_at="2026-02-02T00:00:00Z")
        self.provider.rows = [closed]
        self.page("issues", [closed])
        before = len(self.events())
        # GitHub's Issues feed names the merge only in the nested ``pull_request.merged_at``: ``state`` stays closed.
        merged = pull(state="closed", pull_request={"merged_at": "2026-02-05T00:00:00Z"}, updated_at="2026-02-05T00:00:00Z")
        self.provider.records[("thread", 2)] = {"events": [
            {"type": "ReopenedEvent", "created_at": "2026-02-04T23:59:00Z", "actor": gql(OTHER)},
            {"type": "MergedEvent", "created_at": "2026-02-05T00:00:00Z", "actor": gql(OTHER)},
            {"type": "ClosedEvent", "created_at": "2026-02-05T00:00:00Z", "actor": gql(OTHER)}]}
        self.provider.rows = [merged]
        value = self.page("issues", [merged])
        self.assertEqual(len(self.events()), before + 1)
        self.assertEqual(self.non_text(value), ["Merged the pull request."])
        self.assertEqual(value["actor"]["platform_actor_id"], "33")
        self.assertEqual([r["source"] for r in value["message"]["fact_evidence"]["change_actor"]["records"]], ["MergedEvent"])
        row = self.store.one("SELECT revision,fingerprint FROM snapshots WHERE key LIKE '%:pr:201'")
        # A revision under the fingerprint every release computes for this row: nothing to migrate either way.
        self.assertEqual((row["revision"], row["fingerprint"]), (2, digest(comparison(merged))))
        self.assertNotIn("merged_at", comparison(merged))
        self.page("issues", [merged])  # the merged row again: no second event
        self.assertEqual(len(self.events()), before + 1)

    def test_an_already_merged_pull_request_retained_by_an_earlier_version_is_no_new_event(self):
        self.capable()
        merged = pull(state="closed", pull_request={"merged_at": "2026-02-04T00:00:00Z"}, updated_at="2026-02-04T00:00:00Z")
        key = self.collector.account + ":321:pr:201"
        # The snapshot 0.1.6 stored for it: the same row under the same comparison, its merge only in the raw row.
        earlier = comparison(merged)
        with self.store.transaction() as db:
            db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)", (key, 4, digest(earlier), dump(
                {"comparison": earlier, "text": "Fix\n\nOriginal body", "raw": merged}), "2026-02-04T00:00:05+00:00"))
        self.provider.rows = [merged]
        before = len(self.events())
        self.page("issues", [merged])
        row = self.store.one("SELECT revision,fingerprint,value FROM snapshots WHERE key=?", (key,))
        self.assertEqual(len(self.events()), before)  # an upgrade never replays a merged pull request
        self.assertEqual((row["revision"], row["fingerprint"]), (4, digest(earlier)))
        self.assertFalse(self.provider.record_calls)
        # Its first real change after the upgrade is that change alone, attributed: no merge is invented for it.
        renamed = {**merged, "title": "Fix (follow-up)", "updated_at": "2026-02-06T00:00:00Z"}
        self.provider.records[("thread", 2)] = {"events": [{"type": "RenamedTitleEvent", "created_at": "2026-02-06T00:00:00Z",
                                                            "actor": gql(OTHER), "current_title": "Fix (follow-up)"}]}
        self.provider.rows = [renamed]
        value = self.page("issues", [renamed])
        self.assertEqual(self.non_text(value), ['Changed the title of the pull request from "Fix" to "Fix (follow-up)".'])
        self.assertEqual(value["actor"]["platform_actor_id"], "33")
        self.assertEqual(value["message"]["revision"], 5)

    def test_a_review_request_is_one_event_attributed_from_its_timeline_event(self):
        self.capable()
        spec = {"number": 2, "created_at": "2026-02-01T00:00:00Z"}
        for n, (requester, reviewer) in enumerate(((HUMAN, WRITER), (WRITER, HUMAN))):
            with self.subTest(requester=requester["login"]):
                number = 2 + n
                self.provider.records[("thread", number)] = {"events": [
                    {"type": "ReviewRequestedEvent", "created_at": "2026-02-02T00:00:00Z", "actor": gql(requester),
                     "reviewer": target(reviewer)}]}
                before = len(self.events())
                value = self.page("requests", {"users": [reviewer], "teams": []}, **{**spec, "number": number})
                self.assertEqual(len(self.events()), before + 1)
                self.assertEqual(value["message"]["kind"], "review_request")
                self.assertEqual(value["facts"], ["review_requested"])
                self.assertEqual(value["actor"]["platform_actor_id"], str(requester["id"]))
                self.assertEqual(value["text"], "")
                self.assertEqual(self.non_text(value), ["Requested a review from @" + reviewer["login"] + " (id " + str(reviewer["id"]) + ")."])
        before = len(self.events())
        self.page("requests", {"users": [], "teams": []}, number=9, created_at="2026-02-01T00:00:00Z")
        self.assertEqual(len(self.events()), before)  # nobody was asked: no event

    def requests(self, users, number=2):
        return self.page("requests", {"users": users, "teams": []}, number=number, created_at="2026-02-01T00:00:00Z")

    def test_a_reviewer_who_leaves_the_list_without_a_removal_record_is_not_reread(self):
        self.capable()
        self.provider.records[("thread", 2)] = {"events": [
            {"type": "ReviewRequestedEvent", "created_at": "2026-02-02T00:00:00Z", "actor": gql(OTHER), "reviewer": target(HUMAN)}]}
        self.requests([HUMAN])
        # The reviewer submitted their review: GitHub drops them from the list and keeps no removal event.
        self.provider.records[("thread", 2)] = {"events": []}
        calls = len(self.provider.record_calls)
        value = self.requests([])
        self.assertEqual(value["_state"], "ready")  # offered at once: no reread, nothing held behind it
        self.assertEqual(len(self.provider.record_calls), calls + 1)
        self.assertIsNone(self.store.one("SELECT id FROM jobs WHERE stream='repo:321:enrich'"))
        self.assertEqual(value["facts"], [])
        self.assertEqual(self.non_text(value), ["No longer a requested reviewer: @contributor (id 22)."])
        self.assertEqual(value["actor"], {"platform": "github", "attribution": "unknown"})
        self.assertIn({"scope": "actor", "code": "change_record_missing"}, value["message"]["coverage_gaps"])
        # A later change of the same pull request is not kept waiting behind it.
        merged = pull(state="closed", pull_request={"merged_at": "2026-02-04T00:00:00Z"}, updated_at="2026-02-04T00:00:00Z")
        self.provider.rows = [pull()]
        self.page("issues", [pull()])
        self.provider.records[("thread", 2)] = {"events": [{"type": "MergedEvent", "created_at": "2026-02-04T00:00:00Z", "actor": gql(OTHER)},
                                                           {"type": "ClosedEvent", "created_at": "2026-02-04T00:00:00Z", "actor": gql(OTHER)}]}
        self.provider.rows = [merged]
        self.assertEqual(self.page("issues", [merged])["_state"], "ready")
        self.assertFalse(self.store.rows("SELECT id FROM inbox WHERE state='enriching'"))

    def test_a_removed_request_with_its_record_and_a_request_added_to_a_retained_list_are_attributed(self):
        self.capable()
        now_at = datetime.now(timezone.utc).replace(microsecond=0)

        def at(seconds):
            return (now_at + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        self.provider.records[("thread", 2)] = {"events": [
            {"type": "ReviewRequestedEvent", "created_at": at(-5), "actor": gql(OTHER), "reviewer": target(HUMAN)}]}
        self.requests([HUMAN])
        # A request added to a list already retained: its window runs from the connector's previous observation.
        stale = {"type": "ReviewRequestedEvent", "created_at": at(-86400), "actor": gql(HUMAN), "reviewer": target(WRITER)}
        fresh = {"type": "ReviewRequestedEvent", "created_at": at(0), "actor": gql(OTHER), "reviewer": target(WRITER)}
        self.provider.records[("thread", 2)] = {"events": [stale, fresh]}
        added = self.requests([HUMAN, WRITER])
        self.assertEqual((added["facts"], added["actor"]["platform_actor_id"]), (["review_requested"], "33"))
        self.assertEqual(added["message"]["fact_evidence"]["change_actor"]["records"][0]["at"], fresh["created_at"])
        since = self.provider.record_calls[-1][1]
        self.assertLess(datetime.fromisoformat(since), now_at)  # read from before its previous observation, with slack
        self.assertGreater(datetime.fromisoformat(since), now_at - timedelta(seconds=700))
        # Someone removed a request: GitHub keeps that event, and its actor is the change's.
        self.provider.records[("thread", 2)] = {"events": [
            {"type": "ReviewRequestRemovedEvent", "created_at": at(1), "actor": gql(WRITER), "reviewer": target(HUMAN)}]}
        removed = self.requests([WRITER])
        self.assertEqual((removed["_state"], removed["actor"]["platform_actor_id"]), ("ready", "11"))
        self.assertEqual(self.non_text(removed), ["No longer a requested reviewer: @contributor (id 22)."])
        self.assertEqual(removed["facts"], [])


class DiscussionTests(AttentionCase):
    def discussion(self, **changes):
        return {"id": "D_7", "databaseId": 507, "number": 7, "title": "Idea", "body": "Let's discuss",
                "createdAt": "2026-02-01T00:00:00Z", "updatedAt": "2026-02-01T00:00:00Z",
                "author": gql(OTHER), **changes}

    def observe(self, raw):
        self.provider.rows = [raw]
        self.provider.graph_pages[("discussions", None, None)] = {"nodes": [raw], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        with self.store.transaction() as db:
            db.execute("DELETE FROM jobs")
            self.store.job(db, "repo:321:fixture-discussions", {"kind": "discussions", "repo": REPO})
        self.assertTrue(self.tick_only("repo:321:fixture-discussions"))
        return self.latest()

    def test_a_new_discussion_reports_the_declared_signal_when_the_host_admits_signals(self):
        self.capable(signals=True)
        value = self.observe(self.discussion())
        self.assertIn(SIGNAL_NEW_DISCUSSION, value["facts"])
        self.assertEqual(value["actor"]["platform_actor_id"], "33")  # databaseId, no login lookup
        self.assertNotIn("new_issue", value["facts"])
        self.assertEqual(value["message"]["message_id"], "D_7")
        self.capable(signals=False)
        value = self.observe(self.discussion(id="D_8", databaseId=508, number=8))
        self.assertNotIn(SIGNAL_NEW_DISCUSSION, value["facts"])
        self.capable(signals=True)
        older = self.observe(self.discussion(id="D_9", databaseId=509, number=9, createdAt="2025-12-01T00:00:00Z",
                                             updatedAt="2026-01-05T00:00:00Z"))
        self.assertNotIn(SIGNAL_NEW_DISCUSSION, older["facts"])


    def test_a_closed_discussion_keeps_its_fact_with_an_honestly_unknown_actor(self):
        """GraphQL ``Discussion.closed`` is the one producer of ``closed``; REST issue and pull request rows carry
        ``state`` and no ``closed`` (SOURCES.md). GitHub keeps no record of who closed a discussion."""
        self.capable()
        opened = self.observe(self.discussion(locked=False, closed=False, lastEditedAt=None))
        self.assertEqual(opened["message"]["action"], "observed")
        closed = self.discussion(locked=False, closed=True, lastEditedAt=None, updatedAt="2026-02-03T00:00:00Z")
        self.provider.records[("node", "D_7")] = {"edits": [], "events": []}
        value = self.observe(closed)
        self.assertEqual(value["_state"], "ready")  # no record to wait for: never reread
        self.assertEqual((value["text"], value["facts"]), ("", []))
        self.assertEqual(self.non_text(value), ["Closed the discussion."])  # the fact once, not suppressed
        self.assertEqual(value["actor"], {"platform": "github", "attribution": "unknown"})
        self.assertIn({"scope": "actor", "code": "change_record_missing"}, value["message"]["coverage_gaps"])
        self.assertIsNone(self.store.one("SELECT id FROM jobs WHERE stream='repo:321:enrich'"))

    def test_a_rest_issue_close_is_one_recorded_change(self):
        """The REST issue row as GitHub documents and returns it: ``state``/``state_reason``/``closed_at`` and no
        ``closed`` boolean, so a close is the one ``state`` change its ClosedEvent answers for."""
        rest = {"id": 101, "node_id": "I_101", "number": 1, "title": "Question", "body": "Source text", "user": HUMAN,
                "state": "open", "state_reason": None, "closed_at": None, "locked": False, "assignees": [],
                "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z"}
        closed = {**rest, "state": "closed", "state_reason": "completed", "closed_at": "2026-02-02T00:00:00Z",
                  "updated_at": "2026-02-02T00:00:00Z"}
        self.assertNotIn("closed", comparison(closed))
        old = {"comparison": comparison(rest), "text": "Question\n\nSource text", "raw": rest}
        self.assertEqual(observed_changes("issue", old, closed), [("state", "closed")])
        self.assertEqual(state_changes("issue", old, closed), ["Closed the issue as completed."])


class EnrichmentTests(AttentionCase):
    def setUp(self):
        super().setUp()
        self.provider.rows = [issue(user=OTHER), comment(500, WRITER, "Earlier answer", created="2026-02-01T01:00:00Z")]

    def run_enrichment(self):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0 WHERE stream='repo:321:enrich'")
        return self.tick_only("repo:321:enrich")

    def dispatched(self):
        sent = []
        host = Host(SimpleNamespace(request=lambda method, path, payload: sent.append(payload) or Response(
            200, {"status": "completed", "outcome": "silent"}, {})), self.store, self.outbox)
        host.dispatch()
        host.close()
        return [payload["event"]["source_event_id"] for payload in sent]

    def test_a_transient_history_failure_is_retried_before_the_first_offer(self):
        self.provider.snapshot_errors = [TransportError("read_unavailable")]
        new = comment(600, OTHER, "A follow-up")
        waiting = self.page("comments", [new])
        self.assertEqual(waiting["_state"], "enriching")
        self.assertNotIn("my_thread", waiting["facts"])
        job = self.store.one("SELECT * FROM jobs WHERE stream='repo:321:enrich'")
        self.assertEqual(job["state"], "pending")
        self.assertGreater(job["due"], time.time())
        later = self.page("comments", [comment(601, OTHER, "And another", created="2026-02-02T00:10:00Z")])
        self.assertEqual(later["_state"], "ready")
        # Later new words are never held for another event's read; the waiting event is not offered yet.
        self.assertEqual(self.dispatched(), [later["source_event_id"]])
        self.assertTrue(self.run_enrichment())
        done = self.events()[0]
        self.assertEqual(done["_state"], "ready")
        self.assertEqual(done["source_event_id"], waiting["source_event_id"])
        self.assertIn("my_thread", done["facts"])
        self.assertEqual(done["message"]["raw"], new)
        self.assertEqual(done["conversation"]["thread_history"]["status"], "available")
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.dispatched(), [waiting["source_event_id"]])
        self.assertIsNone(self.store.meta("enrich:" + waiting["source_event_id"]))

    def test_retries_are_bounded_and_end_with_an_explicit_gap(self):
        self.provider.snapshot_errors = [TransportError("read_unavailable")] * (ENRICH_ATTEMPTS + 2)
        waiting = self.page("comments", [comment(600, OTHER, "A follow-up")])
        runs = 0
        while self.events()[0]["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.run_enrichment()
        self.assertEqual(runs, ENRICH_ATTEMPTS - 1)
        done = self.events()[0]
        self.assertNotIn("my_thread", done["facts"])
        self.assertIn({"scope": "history", "code": "read_unavailable"}, done["message"]["coverage_gaps"])
        self.assertEqual(done["conversation"]["thread_history"], {"status": "unavailable", "observed_at": done["conversation"]["thread_history"]["observed_at"], "error": "read_unavailable"})
        self.assertEqual(done["source_event_id"], waiting["source_event_id"])

    def test_a_permanent_failure_does_not_wait(self):
        self.provider.snapshot_errors = [TransportError("no_longer_visible", Response(404, {}, {}))]
        value = self.page("comments", [comment(600, OTHER, "A follow-up")])
        self.assertEqual(value["_state"], "ready")
        self.assertIn({"scope": "history", "code": "no_longer_visible"}, value["message"]["coverage_gaps"])
        self.assertIsNone(self.store.one("SELECT id FROM jobs WHERE stream='repo:321:enrich'"))

    def test_a_retained_watched_opening_proves_my_thread_without_waiting(self):
        self.provider.rows = [issue(user=WRITER)]
        self.page("issues", [issue(user=WRITER)])
        self.provider.snapshot_errors = [TransportError("read_unavailable")]
        value = self.page("comments", [comment(600, OTHER, "Hello")])
        self.assertEqual(value["_state"], "ready")
        self.assertIn("my_thread", value["facts"])
        self.assertEqual(value["message"]["fact_evidence"]["my_thread"][-1]["source"], "retained_thread_opening")

    def test_a_transient_change_record_failure_is_retried_then_attributed(self):
        self.capable()
        self.provider.rows = [issue()]
        self.page("issues", [issue()])
        closed = issue(state="closed", updated_at="2026-02-02T00:00:00Z")
        self.provider.rows = [closed]
        self.provider.records[("thread", 1)] = [TransportError("graphql_http_502", Response(502, {}, {})),
                                                {"events": [{"type": "ClosedEvent", "created_at": closed["updated_at"], "actor": gql(OTHER)}]}]
        waiting = self.page("issues", [closed])
        self.assertEqual(waiting["_state"], "enriching")
        self.assertTrue(self.run_enrichment())
        done = self.latest()
        self.assertEqual((done["_state"], done["actor"]["platform_actor_id"]), ("ready", "33"))
        self.assertEqual(done["source_event_id"], waiting["source_event_id"])

    def test_waiting_custody_survives_restart_and_an_offered_event_is_never_rewritten(self):
        self.provider.snapshot_errors = [TransportError("read_unavailable")]
        waiting = self.page("comments", [comment(600, OTHER, "A follow-up")])
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.outbox.store = self.store
        self.collector = Collector(self.store, self.provider, CONFIG)
        self.assertEqual(self.store.one("SELECT state FROM inbox WHERE id=?", (waiting["source_event_id"],))["state"], "enriching")
        self.assertTrue(self.run_enrichment())
        self.assertEqual(self.store.one("SELECT state FROM inbox WHERE id=?", (waiting["source_event_id"],))["state"], "ready")
        offered = self.store.one("SELECT event FROM inbox WHERE id=?", (waiting["source_event_id"],))["event"]
        with self.store.transaction() as db:  # a stray retry record for an already offered event
            db.execute("UPDATE inbox SET state='accepted' WHERE id=?", (waiting["source_event_id"],))
            db.execute("INSERT INTO meta VALUES(?,?)", ("enrich:" + waiting["source_event_id"], json.dumps(
                {"factory": "content", "kwargs": {}, "old": None, "needs": [{"need": "history"}], "attempts": 1, "first_at": 0})))
            self.store.job(db, "repo:321:enrich", {"kind": "enrich", "repo": REPO, "event_id": waiting["source_event_id"]})
        self.run_enrichment()
        self.assertEqual(self.store.one("SELECT event FROM inbox WHERE id=?", (waiting["source_event_id"],))["event"], offered)


class OutsideCase(AttentionCase):
    """Outside-repository notifications for two readers (11 and 22) of one public pull request thread."""

    def setUp(self):
        super().setUp()
        self.reader = FakeProvider()
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, {11: self.reader, 22: self.reader})
        self.store.set_meta("bootstrap:notifications:22", "2026-01-01T00:00:00+00:00")
        self.store.set_meta("bootstrap:notifications:11", "2026-01-01T00:00:00+00:00")
        self.provider.pages["https://api.github.com/repos/example/project/pulls/2"] = Response(200, pull(), {})
        self.provider.rows = [pull()]

    def notification(self, recipient=22, updated="2026-02-02T00:00:01Z", reason="subscribed", **subject):
        return {"id": "N" + str(recipient), "reason": reason, "updated_at": updated,
                "repository": {**REPO, "private": False},
                "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/example/project/pulls/2", **subject}}

    def discover(self, recipient=22, previous="2026-02-01T00:00:00Z", **notification):
        stream = "notifications:" + str(recipient) + ":children"
        spec = {"kind": "notification_discovery", "recipient": recipient, "notification": self.notification(recipient, **notification)}
        if previous:
            spec["previous_updated_at"] = previous
        with self.store.transaction() as db:
            self.store.job(db, stream, spec)
        self.assertTrue(self.tick_only(stream))
        return self.envelope(recipient)

    def envelope(self, recipient=22):
        """This reader's latest notification observation (no facts of its own under the change capability)."""
        rows = [e for e in self.events() if e["message"].get("discovery_only") and e["message"].get("recipient_id") == recipient
                and "reader" not in e["message"]["fact_evidence"]]
        return rows[-1] if rows else None

    def items(self):
        """The proven outside messages and actions: one event each, whichever reader revealed it."""
        return [e for e in self.events() if e["message"].get("discovery_only") and "reader" in e["message"]["fact_evidence"]]

    def enrich(self, recipient=22):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0 WHERE stream=?", ("notifications:" + str(recipient) + ":enrich",))
        return self.tick_only("notifications:" + str(recipient) + ":enrich")

    def requested(self, reviewer, at="2026-02-02T00:00:00Z", kind="User", ident="RRE_1"):
        self.provider.records[("thread", 2)] = {"events": [
            {"type": "ReviewRequestedEvent", "id": ident, "created_at": at, "actor": gql(OTHER), "reviewer": target(reviewer, kind)}]}

    def mention_events(self):
        return [item for item in self.items() if "mention" in item["facts"]]


class OutsideNotificationTests(OutsideCase):
    def test_a_proven_outside_review_request_names_its_requester(self):
        self.capable()
        self.requested(HUMAN)
        envelope = self.discover(reason="review_requested")
        item, = self.items()
        self.assertTrue(item["message"]["discovery_only"])
        self.assertEqual(item["facts"], ["review_requested"])
        self.assertEqual(item["actor"]["platform_actor_id"], "33")
        self.assertEqual(item["text"], "")
        self.assertTrue(self.non_text(item, "notification")[0].startswith("GitHub notification for @contributor (id 22), reason review_requested"))
        self.assertEqual(self.non_text(item), ["Requested a review from @contributor (id 22), by @visitor (id 33)."])
        self.assertIn({"scope": "permission", "code": "repository_not_selected_for_writes"}, item["message"]["coverage_gaps"])
        # The notification itself is this reader's evidence: no facts, no revision link, a background change.
        self.assertEqual((envelope["facts"], envelope["text"], envelope["_state"]), ([], "", "ready"))
        self.assertEqual(envelope["actor"]["attribution"], "notification_service")
        self.assertEqual(envelope["message"]["fact_evidence"]["items"], [item["source_event_id"]])
        self.assertTrue(self.non_text(envelope))

    def test_stale_team_and_unwatched_requests_prove_nothing(self):
        self.capable()
        for setup in (lambda: self.requested(HUMAN, at="2026-01-20T00:00:00Z"),
                      lambda: self.requested({"id": 22, "login": "core-team"}, kind="Team"),
                      lambda: self.requested(OTHER)):
            with self.subTest():
                setup()
                with self.store.transaction() as db:  # a fresh fixture per case, not a provider revision
                    db.execute("DELETE FROM snapshots")
                    db.execute("DELETE FROM jobs")
                    db.execute("DELETE FROM inbox WHERE id!='event-1'")
                envelope = self.discover()
                self.assertEqual(self.items(), [])
                self.assertEqual(envelope["facts"], [])
                self.assertEqual(envelope["actor"]["attribution"], "notification_service")

    def test_a_proven_outside_mention_marks_the_mentioning_comment(self):
        self.capable()
        mention = {"id": 909, "node_id": "IC_909", "body": "@contributor could you look?", "user": OTHER,
                   "created_at": "2026-02-02T00:00:00Z", "updated_at": "2026-02-02T00:00:00Z",
                   "issue_url": "https://api.github.com/repos/example/project/issues/2"}
        self.provider.rows = [pull(), mention]
        self.discover()
        item, = self.items()
        self.assertEqual(item["facts"], ["mention"])
        self.assertEqual(item["message"]["message_id"], "IC_909")
        self.assertEqual(item["message"]["mentions"], [{"id": "22", "name": "contributor", "is_self": False}])
        self.assertIn("IC_909", [m["message_id"] for m in item["conversation"]["thread_history"]["data"]["messages"]])
        self.assertEqual(item["actor"]["platform_actor_id"], "33")
        self.assertEqual(item["message"]["action"], "observed")
        self.assertIsNone(item["message"]["previous_source_event_id"])
        self.assertEqual(self.non_text(item), [])  # new words: no change marker

    def test_a_transient_proof_failure_is_retried_before_the_first_offer(self):
        self.capable()
        self.provider.records[("thread", 2)] = [TransportError("provider_http_502", Response(502, {}, {})), {"events": [
            {"type": "ReviewRequestedEvent", "id": "RRE_2", "created_at": "2026-02-02T00:00:00Z", "actor": gql(OTHER),
             "reviewer": target(HUMAN)}]}]
        waiting = self.discover()
        self.assertEqual((waiting["_state"], self.items()), ("enriching", []))
        self.assertTrue(self.enrich())
        item, = self.items()
        done = self.envelope()
        self.assertEqual(done["source_event_id"], waiting["source_event_id"])
        self.assertEqual((done["_state"], done["facts"]), ("ready", []))
        self.assertEqual((item["_state"], item["facts"], item["actor"]["platform_actor_id"]), ("ready", ["review_requested"], "33"))
        self.assertEqual(done["message"]["fact_evidence"]["items"], [item["source_event_id"]])

    def test_a_host_without_the_capability_keeps_the_sticky_reason_rule(self):
        self.requested(HUMAN)
        value = self.discover(reason="review_requested")
        self.assertEqual(value["facts"], ["any_message"])
        self.assertEqual(value["actor"]["attribution"], "notification_service")
        self.assertEqual(self.items(), [])
        self.assertFalse(self.provider.record_calls)


class GraphQLShapeTests(unittest.TestCase):
    def test_change_queries_use_the_checked_schema_names(self):
        issue_part, pull_part = THREAD_CHANGES.split("... on PullRequest")
        self.assertIn("issueOrPullRequest(number:$number)", THREAD_CHANGES)
        self.assertIn("issueTimeline: timelineItems(since:$since,last:100", issue_part)
        self.assertIn("pullTimeline: timelineItems(since:$since,last:100", pull_part)
        for name in ("REVIEW_REQUESTED_EVENT", "MERGED_EVENT", "READY_FOR_REVIEW_EVENT", "ReviewRequestedEvent"):
            self.assertNotIn(name, issue_part)
            self.assertIn(name, pull_part)
        for text in (THREAD_CHANGES, NODE_CHANGES):
            # GitHub lists content edits newest first: ``first`` is the newest window (SOURCES.md).
            self.assertIn("userContentEdits(first: 50) { pageInfo { hasNextPage }", text)
            self.assertNotIn("userContentEdits(last", text)
            self.assertIn("... on Bot { databaseId }", text)
            self.assertEqual(text.count("{"), text.count("}"))
        self.assertIn("... on Comment", NODE_CHANGES)

    def test_the_edit_record_of_an_object_edited_more_than_fifty_times_is_its_newest_window(self):
        """GitHub lists content edits newest first (one live public read, SOURCES.md): ``first: 50`` is the newest
        window with ``hasNextPage`` for the older rest; ``last: 50`` would be the oldest fifty, without the edit
        just observed."""
        total = 60
        newest_first = [{"editedAt": "2026-03-01T00:%02d:00Z" % (total - 1 - n), "deletedAt": None,
                         "editor": gql(OTHER if n == 0 else HUMAN)} for n in range(total)]

        def connection(query):
            end, size = re.search(r"userContentEdits\((first|last): (\d+)\)", query).groups()
            size = int(size)
            if end == "first":
                return {"pageInfo": {"hasNextPage": size < total, "hasPreviousPage": False}, "nodes": newest_first[:size]}
            return {"pageInfo": {"hasNextPage": False, "hasPreviousPage": size < total}, "nodes": newest_first[-size:]}

        def dispatch(method, path, data):
            item = {"__typename": "Issue", "lastEditedAt": newest_first[0]["editedAt"],
                    "userContentEdits": connection(data["query"]), "issueTimeline": {"pageInfo": {}, "nodes": []}}
            return 200, {"data": {"repository": {"issueOrPullRequest": item}}}, {}
        server = Loopback(dispatch)
        self.addCleanup(server.close)
        import tempfile
        scratch = tempfile.TemporaryDirectory(prefix="github-edit-window-")
        self.addCleanup(scratch.cleanup)
        store = Store(scratch.name)
        self.addCleanup(store.close)
        provider = GitHub(HTTP(server.url, "synthetic-writer", test_loopback=True), store)
        record = provider.change_record({"repo": REPO, "number": 1}, since="2026-03-01T00:00:00Z")
        self.assertIn("userContentEdits(first: 50)", server.requests[0][2]["query"])
        self.assertEqual(len(record["edits"]), 50)
        self.assertTrue(record["edits_incomplete"])
        newest = newest_first[0]["editedAt"]
        self.assertEqual(record["edits"][0]["edited_at"], newest)  # the edit just observed is inside the window
        before = issue(body="Before", updated_at=newest_first[1]["editedAt"])
        after = issue(body="After", updated_at=newest)
        old = {"comparison": comparison(before), "text": "Question\n\nBefore", "raw": before}
        result = attribute("issue", old, after, record, provider.actor_of, lower=before["updated_at"], upper=newest)
        self.assertEqual((result["status"], result["actor"]["platform_actor_id"]), ("attributed", "33"))
        self.assertEqual(result["word_editors"], {"ids": ["22", "33"], "complete": False})  # older edits are outside it
        # The other end of the same connection: the oldest fifty, which do not contain that edit at all.
        oldest = connection(THREAD_CHANGES.replace("userContentEdits(first: 50)", "userContentEdits(last: 50)"))
        self.assertNotIn(newest, [edit["editedAt"] for edit in oldest["nodes"]])
        self.assertTrue(oldest["pageInfo"]["hasPreviousPage"])
        # A window that lies wholly inside the observed interval may be missing its other edits: never attributed.
        wide = attribute("issue", old, after, record, provider.actor_of, lower="2026-01-01T00:00:00Z", upper=newest)
        self.assertEqual((wide["status"], wide["code"]), ("unresolved", "change_record_incomplete"))

    def test_change_record_normalizes_a_real_graphql_response(self):
        seen = []
        edits = {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"editedAt": "2026-02-02T00:00:00Z", "deletedAt": None, "editor": gql(WRITER)}]}
        nodes = [{"__typename": "ReviewRequestedEvent", "id": "RRE_9", "createdAt": "2026-02-02T00:00:00Z", "actor": gql(HUMAN),
                  "requestedReviewer": {"__typename": "User", "databaseId": 11, "login": "service-writer"}},
                 {"__typename": "AssignedEvent", "createdAt": "2026-02-02T00:00:01Z", "actor": gql(HUMAN),
                  "assignee": {"__typename": "Bot", "databaseId": 901, "login": "helper-app"}}]

        def dispatch(method, path, data):
            seen.append(data)
            if "issueOrPullRequest" in data["query"]:
                item = {"__typename": "PullRequest", "lastEditedAt": "2026-02-02T00:00:00Z", "userContentEdits": edits,
                        "pullTimeline": {"pageInfo": {"hasPreviousPage": True}, "nodes": nodes}}
                return 200, {"data": {"repository": {"issueOrPullRequest": item}}}, {}
            return 200, {"data": {"node": {"__typename": "IssueComment", "userContentEdits": edits}}}, {}
        server = Loopback(dispatch)
        self.addCleanup(server.close)
        import tempfile
        scratch = tempfile.TemporaryDirectory(prefix="github-change-record-")
        self.addCleanup(scratch.cleanup)
        store = Store(scratch.name)
        self.addCleanup(store.close)
        provider = GitHub(HTTP(server.url, "synthetic-writer", test_loopback=True), store)
        record = provider.change_record({"repo": REPO, "number": 2}, since="2026-02-01T00:00:00Z")
        self.assertEqual(seen[0]["variables"], {"owner": "example", "name": "project", "number": 2, "since": "2026-02-01T00:00:00Z",
                                                "before": None})
        self.assertEqual(record["typename"], "PullRequest")
        self.assertTrue(record["events_incomplete"])
        self.assertFalse(record["edits_incomplete"])
        self.assertEqual(record["events"][0]["reviewer"], {"typename": "User", "id": 11, "login": "service-writer"})
        self.assertEqual(record["events"][0]["id"], "RRE_9")
        self.assertEqual(record["events"][1]["assignee"], {"typename": "Bot", "id": 901, "login": "helper-app"})
        self.assertEqual(provider.actor_of(record["edits"][0]["editor"])["platform_actor_id"], "11")
        node = provider.change_record({"repo": REPO}, node="IC_1")
        self.assertEqual(seen[1]["variables"], {"id": "IC_1"})
        self.assertEqual(node["events"], [])
        self.assertEqual(len(node["edits"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
