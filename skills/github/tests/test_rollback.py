"""Returning to an earlier release (SETUP.md): the release step leaves nothing that release cannot read.

0.1.6 reads this state in two places, pinned below from its published source (``gh_presence/host.py`` dispatch and
``gh_presence/collector.py`` tick of the hub's 0.1.6 payload). It has no ``enriching`` inbox state and no ``enrich``
job: left as they are, a waiting event is never offered and its job fails every minute. Synthetic data only.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from test_attention_repair import OTHER, OutsideCase, comment, issue, pull
from test_transport import CONFIG, WRITER
from gh_presence.collector import Collector
from gh_presence.config import write_config
from gh_presence.http import Response, TransportError
from gh_presence.locking import CompanionLock
from gh_presence.store import Store

ROOT = Path(__file__).resolve().parents[1]
# 0.1.6 host.py: "SELECT * FROM inbox WHERE state IN ('ready','blocked') AND due<=? ORDER BY seq LIMIT ?"
OFFERED_BY_0_1_6 = "SELECT id FROM inbox WHERE state IN ('ready','blocked') AND due<=? ORDER BY seq"
# Every inbox state 0.1.6 writes or reads (store.py recover/status, host.py accept, collector baseline/echo).
STATES_0_1_6 = {"ready", "blocked", "submitting", "accepted", "retained", "rejected", "baseline", "echo"}
KINDS_0_1_6 = {"issues", "comments", "inline", "discussions", "discussion_comments", "discussion_replies", "reviews",
               "requests", "pull", "notifications", "notification_discovery"}


def read_by_0_1_6(spec):
    """0.1.6 collector.py tick: a job is a notification discovery, a GraphQL discussion page, or a REST page it reads
    from ``spec["path"]``; any other spec raises there and is retried every minute."""
    kind = spec.get("kind")
    return kind in KINDS_0_1_6 and (kind == "notification_discovery" or kind.startswith("discussion") or "path" in spec)


def unavailable(status=503):
    return TransportError("provider_http_" + str(status), Response(status, {}, {}))


class ReleaseCase(OutsideCase):
    def setUp(self):
        super().setUp()
        self.collector = Collector(self.store, self.provider, CONFIG, {11: self.reader, 22: self.reader})
        self.capable()

    def wait_for_three_reads(self):
        """One waiting event of each form: new words whose thread and author were not read, a change whose record was
        not read, and an outside notification whose timeline was not read."""
        self.provider.rows = [issue(user=OTHER), comment(500, WRITER, "Earlier answer", created="2026-02-01T01:00:00Z")]
        self.page("issues", [issue(user=OTHER)])
        self.provider.pages["/users/lookup-user"] = unavailable()
        self.provider.snapshot_errors = [TransportError("read_unavailable")]
        words = self.page("comments", [{**comment(600, None, "A follow-up"), "user": {"login": "lookup-user", "type": "User"}}])
        closed = issue(user=OTHER, state="closed", updated_at="2026-02-02T00:00:00Z")
        self.provider.rows = [closed]
        self.provider.records[("thread", 1)] = [unavailable(502)]
        change = self.page("issues", [closed])
        self.provider.rows = [pull(), {"id": 909, "node_id": "IC_909", "body": "@contributor could you look?", "user": OTHER,
                                       "created_at": "2026-02-02T00:00:00Z", "updated_at": "2026-02-02T00:00:00Z",
                                       "issue_url": "https://api.github.com/repos/example/project/issues/2"}]
        self.provider.records[("thread", 2)] = [unavailable(502)]
        notification = self.discover()
        for value in (words, change, notification):
            self.assertEqual(value["_state"], "enriching")
        return words, change, notification

    def states(self):
        return {row["id"]: row["state"] for row in self.store.rows("SELECT id,state FROM inbox")}

    def pending_jobs(self):
        return [json.loads(row["spec"]) for row in self.store.rows("SELECT spec FROM jobs WHERE state='pending'")]

    def offered(self):
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET due=0")
        return [row["id"] for row in self.store.rows(OFFERED_BY_0_1_6, (10 ** 12,))]


class ReleaseTests(ReleaseCase):
    def test_without_the_release_step_an_earlier_release_cannot_read_waiting_custody(self):
        waiting = self.wait_for_three_reads()
        offered = self.offered()
        for value in waiting:
            self.assertNotIn(value["source_event_id"], offered)  # 0.1.6 would never offer them
        self.assertTrue(set(self.states().values()) - STATES_0_1_6)
        unread = [spec for spec in self.pending_jobs() if not read_by_0_1_6(spec)]
        self.assertEqual(sorted(spec["kind"] for spec in unread), ["enrich", "enrich", "enrich"])  # and would fail on these

    def test_the_release_step_completes_every_waiting_event_with_its_open_reads_as_gaps(self):
        words, change, notification = self.wait_for_three_reads()
        mention, = self.mention_events()  # proven and sent before the timeline failed: never rewritten
        reads = (len(self.provider.record_calls), len(self.provider.snapshot_errors))
        self.provider.pages["/users/lookup-user"] = AssertionError("the release step reads nothing")
        released = self.collector.release_waiting()
        self.assertEqual(sorted(released), sorted(value["source_event_id"] for value in (words, change, notification)))
        self.assertEqual((len(self.provider.record_calls), len(self.provider.snapshot_errors)), reads)
        # Nothing is left in a form 0.1.6 does not read.
        self.assertTrue(set(self.states().values()) <= STATES_0_1_6)
        self.assertEqual([spec for spec in self.pending_jobs() if not read_by_0_1_6(spec)], [])
        self.assertFalse(self.store.rows("SELECT key FROM meta WHERE key LIKE 'enrich:%'"))
        offered = self.offered()
        events = {value["source_event_id"]: value for value in self.events()}
        for value in (words, change, notification):
            self.assertIn(value["source_event_id"], offered)
        done = events[words["source_event_id"]]
        gaps = done["message"]["coverage_gaps"]
        self.assertEqual(done["message"]["raw"], words["message"]["raw"])
        self.assertEqual(done["message"]["observed_at"], words["message"]["observed_at"])
        self.assertIn({"scope": "history", "code": "read_unavailable"}, gaps)
        self.assertIn({"scope": "actor", "code": "provider_http_503"}, gaps)
        self.assertIn({"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["author", "history"]}, gaps)
        self.assertNotIn("my_thread", done["facts"])  # never claimed from a thread that was not read
        done = events[change["source_event_id"]]
        self.assertEqual((done["text"], done["actor"]), ("", {"platform": "github", "attribution": "unknown"}))
        self.assertIn({"scope": "actor", "code": "provider_http_502"}, done["message"]["coverage_gaps"])
        self.assertIn({"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["attribution"]},
                      done["message"]["coverage_gaps"])
        done = events[notification["source_event_id"]]
        self.assertEqual(done["message"]["fact_evidence"]["items"], [mention["source_event_id"]])
        self.assertIn({"scope": "notification", "code": "addressed_action_unverified", "need": "thread_record",
                       "error": "provider_http_502"}, done["message"]["coverage_gaps"])  # its last real failure
        self.assertIn({"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["thread_record"]},
                      done["message"]["coverage_gaps"])
        self.assertEqual(json.dumps(events[mention["source_event_id"]], sort_keys=True), json.dumps(mention, sort_keys=True))
        self.assertEqual(self.collector.release_waiting(), [])  # nothing left to release

    def test_a_released_event_keeps_what_its_retries_already_read(self):
        self.provider.rows = [issue(user=OTHER), comment(500, WRITER, "Earlier answer", created="2026-02-01T01:00:00Z")]
        self.page("issues", [issue(user=OTHER)])
        self.provider.pages["/users/lookup-user"] = unavailable()
        self.provider.snapshot_errors = [TransportError("read_unavailable")]
        new = {**comment(600, None, "A follow-up"), "user": {"login": "lookup-user", "type": "User"}}
        self.provider.rows.append(new)
        waiting = self.page("comments", [new])
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0 WHERE stream='repo:321:enrich'")
        self.assertFalse(self.tick_only("repo:321:enrich"))  # the thread is read; the author still is not
        self.collector.release_waiting()
        done = next(value for value in self.events() if value["source_event_id"] == waiting["source_event_id"])
        self.assertEqual(done["_state"], "ready")
        self.assertIn("my_thread", done["facts"])
        self.assertIn({"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["author"]},
                      done["message"]["coverage_gaps"])

    def test_the_release_command_runs_only_while_no_companion_holds_the_state(self):
        self.wait_for_three_reads()
        write_config(self.scratch.name, CONFIG)
        self.store.close()
        env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
               "OUROBOROS_SKILL_STATE_DIR": self.scratch.name}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        command = [sys.executable, "-B", str(ROOT / "scripts" / "companion.py"), "--release-waiting"]
        with CompanionLock(Path(self.scratch.name) / "companion.lock"):  # a companion still runs: refused, nothing changed
            held = subprocess.run(command, capture_output=True, text=True, env=env, cwd=self.scratch.name, timeout=60)
        self.assertNotEqual(held.returncode, 0)
        self.assertIn("another github companion holds this state", held.stderr)
        done = subprocess.run(command, capture_output=True, text=True, env=env, cwd=self.scratch.name, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual((len(report["released"]), report["still_enriching"], report["enrich_jobs_pending"]), (3, 0, 0))
        self.assertGreaterEqual(report["undelivered_change_observations"], 2)  # the change and the notification itself
        self.store = Store(self.scratch.name)
        self.assertTrue(set(self.states().values()) <= STATES_0_1_6)
        self.assertEqual([spec for spec in self.pending_jobs() if not read_by_0_1_6(spec)], [])
        unknown = subprocess.run(command[:-1] + ["--else"], capture_output=True, text=True, env=env, cwd=self.scratch.name, timeout=60)
        self.assertNotEqual(unknown.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
