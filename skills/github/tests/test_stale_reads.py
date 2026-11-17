"""A copy of a thread read earlier never decides what a later read observed (synthetic accounts only).

Every scenario runs the collector's own jobs: a reader's Notifications feed page, the discovery job that page queues,
its enrichment retries, and a restart between them. Two readers (11 and 22) watch one public pull request thread.
A notification still waiting when the thread is read again proves from that newest read, as the notification that
made it keeps it; a copy a later read superseded is never proven from, on any attempt or at release, and stays
what that notification itself read.
"""
import json
import time
import unittest
from datetime import datetime, timedelta, timezone

from test_attention_repair import OTHER, OutsideCase, gql, pull
from test_transport import CONFIG
from gh_presence.collector import ENRICH_ATTEMPTS, Collector
from gh_presence.http import Response, TransportError
from gh_presence.store import Store, dump

MAINTAINER = {"id": 55, "login": "maintainer", "type": "User"}
LOOKUP = "/users/lookup-user"  # an account GitHub names by login only: its number is one more read
ISSUE_URL = "https://api.github.com/repos/example/project/issues/2"
WITH, WITHOUT = "@contributor please look", "please look"


def unavailable(status=502):
    return TransportError("provider_http_" + str(status), Response(status, {}, {}))


def stamp(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def between_reads():
    """A provider time strictly between the connector's previous read of a thread and its next one, on any clock."""
    time.sleep(0.02)
    at = datetime.now(timezone.utc).isoformat()
    time.sleep(0.02)
    return at


def revision(at, user):
    return {"edited_at": at, "deleted": False,
            "editor": gql(user) if "id" in user else {"login": user["login"], "__typename": "User"}}


class FeedCase(OutsideCase):
    def setUp(self):
        super().setUp()
        self.capable()
        self.base = datetime.now(timezone.utc) - timedelta(days=1)
        for reader in (11, 22):
            self.store.set_meta("bootstrap:notifications:" + str(reader), stamp(self.base))
        self.written = stamp(self.base + timedelta(hours=1))
        self.polls = self.thread_reads = 0
        self.provider.records[("thread", 2)] = {"events": []}
        read = self.provider.thread_snapshot

        def counted(route):
            self.thread_reads += 1
            return read(route)
        self.provider.thread_snapshot = counted

    def reset(self):
        """A clean state for the next variant of one scenario."""
        with self.store.transaction() as db:
            for table in ("snapshots", "jobs", "notifications"):
                db.execute("DELETE FROM " + table)
            db.execute("DELETE FROM inbox WHERE id!='event-1'")
            db.execute("DELETE FROM meta WHERE key LIKE 'enrich:%'")
        self.provider.records = {("thread", 2): {"events": []}}
        self.provider.snapshot_errors = []
        self.provider.pages.pop(LOOKUP, None)
        self.provider.users.clear()

    def restart(self):
        """A new companion process over the same state."""
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.outbox.store = self.store
        self.provider.users.clear()
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, {11: self.reader, 22: self.reader})

    def run_job(self, job):
        """The real tick() on exactly this job; every other pending job is parked meanwhile."""
        others = [r["id"] for r in self.store.rows("SELECT id FROM jobs WHERE state='pending' AND id!=?", (job,))]
        with self.store.transaction() as db:
            db.executemany("UPDATE jobs SET state='parked' WHERE id=?", [(i,) for i in others])
            db.execute("UPDATE jobs SET due=0 WHERE id=?", (job,))
        try:
            return self.collector.tick()
        finally:
            with self.store.transaction() as db:
                db.executemany("UPDATE jobs SET state='pending' WHERE id=? AND state='parked'", [(i,) for i in others])

    def poll(self, reader, **fields):
        """This reader's Notifications feed shows its notification of the thread updated now: the feed job, as the
        collector runs it. The id of the discovery job it queued."""
        self.polls += 1
        path = "/fixture/notifications/%d/%d" % (reader, self.polls)
        self.reader.pages[path] = Response(200, [self.notification(reader, updated=between_reads(), **fields)], {})
        stream = "notifications:" + str(reader)
        with self.store.transaction() as db:
            self.store.job(db, stream, {"kind": "notifications", "recipient": reader, "path": path})
        self.assertTrue(self.tick_only(stream))
        return self.store.one("SELECT id FROM jobs WHERE stream=? AND state='pending' ORDER BY id DESC LIMIT 1",
                              (stream + ":children",))["id"]

    def notify(self, reader, **fields):
        """A feed poll and the discovery job it queued."""
        return self.run_job(self.poll(reader, **fields))

    def retry(self, reader):
        """This reader's waiting notification, tried again."""
        job = self.store.one("SELECT id FROM jobs WHERE stream=? AND state='pending' ORDER BY id LIMIT 1",
                             ("notifications:" + str(reader) + ":enrich",))
        return self.run_job(job["id"])

    def comment(self, ident=980, body=WITH, user=OTHER):
        self.edits = [revision(self.written, user)]  # GitHub's oldest content edit: the original revision
        row = {"id": ident, "node_id": "IC_" + str(ident), "body": body, "user": user, "created_at": self.written,
               "updated_at": self.written, "issue_url": ISSUE_URL}
        self.provider.rows = [pull(), row]
        return row

    def edit(self, row, body, editor):
        """An edit of the comment between two reads of its thread, with GitHub's record of it."""
        at = between_reads()
        self.edits.insert(0, revision(at, editor))
        changed = {**row, "body": body, "updated_at": at}
        self.provider.rows = [pull(), changed]
        self.provider.records[("node", row["node_id"])] = {"edits": list(self.edits)}
        return changed

    def retained(self, row):
        """What the store keeps of this message: its revision, its words and when they were last observed."""
        found = self.store.one("SELECT revision,value,observed FROM snapshots WHERE key LIKE ?", ("%:outside:%" + row["node_id"],))
        return found["revision"], json.loads(found["value"])["text"], found["observed"]

    def waiting_event(self):
        return next(e for e in self.events() if e["_state"] == "enriching")

    def record_of(self, waiting):
        return self.store.meta("enrich:" + waiting["source_event_id"])

    def state(self, event_id):
        found = next(e for e in self.events() if e["source_event_id"] == event_id)
        return found["_state"], found

    def retry_until_done(self, waiting, reader=22):
        """This reader's waiting notification, tried until it goes out; how many tries that took."""
        runs = 0
        while self.state(waiting["source_event_id"])[0] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.retry(reader)
        return runs

    def unkeep_newest(self, waiting):
        """The newest read of the thread is kept by no notification observation any more (the one that made it is
        gone): nothing the collector itself does, the state its last line of defence is for."""
        holder = self.record_of(waiting)["superseded"]["holder"]
        with self.store.transaction() as db:
            db.execute("DELETE FROM inbox WHERE id=?", (holder,))

    def gaps(self, event_id):
        return [gap for gap in self.state(event_id)[1]["message"]["coverage_gaps"] if gap["scope"] != "permission"]


class WaitingCopyTests(FeedCase):
    """Reader 22's notification A reads the thread, then waits for an unrelated read; another notification B reads
    it later. A's copy is no longer the newest: A proves from the read B keeps."""

    def sequence(self, later_reader=11):
        """Reported with the mention; the mention edited away and read by A, which then waits; the mention added
        back by a maintainer and reported by B as revision 2."""
        row = self.comment()
        self.assertTrue(self.notify(22))
        reported, = self.mention_events()
        row = self.edit(row, WITHOUT, OTHER)
        self.provider.records[("thread", 2)] = [unavailable()]  # A's timeline read fails: an unrelated dependency
        self.assertTrue(self.notify(22))
        self.waiting = self.waiting_event()
        self.assertEqual(self.retained(row)[:2], (1, WITHOUT))  # A's own, real observation of the removal
        self.assertNotIn("superseded", self.record_of(self.waiting))
        self.provider.records[("thread", 2)] = {"events": []}
        row = self.edit(row, WITH, MAINTAINER)
        self.assertTrue(self.notify(later_reader))
        again = self.mention_events()[-1]
        self.assertEqual((again["message"]["revision"], again["actor"]["platform_actor_id"]), (2, "55"))
        self.assertEqual(self.retained(row)[:2], (2, WITH))
        # B's read told A, in the waiting record itself: when, and which observation keeps those rows.
        told = self.record_of(self.waiting)["superseded"]
        self.assertEqual(told, {"at": self.retained(row)[2], "holder": self.envelope(later_reader)["source_event_id"]})
        return row, [reported, again]

    def settled(self, row, sent, observed):
        """Nothing was rewound, duplicated or rewritten, whichever reader looks next."""
        revision, words, at = self.retained(row)
        self.assertEqual((revision, words), (2, WITH))
        self.assertGreaterEqual(at, observed)
        for reader in (22, 11, 22):
            self.assertTrue(self.notify(reader))
            self.assertEqual(len(self.mention_events()), 2)  # never a third event for the mention B reported
        self.assertEqual([dump(event) for event in self.mention_events()], [dump(event) for event in sent])
        self.assertEqual(self.retained(row)[:2], (2, WITH))

    def test_a_waiting_notification_proves_from_the_newest_read_without_reading_the_thread_again(self):
        for later_reader in (11, 22):  # another reader's notification, or this reader's own next update
            with self.subTest(later_reader=later_reader):
                self.reset()
                row, sent = self.sequence(later_reader)
                observed = self.retained(row)[2]
                self.restart()
                reads = self.thread_reads
                self.provider.snapshot_errors = [unavailable()] * (ENRICH_ATTEMPTS + 2)  # unreadable now: never asked
                self.assertTrue(self.retry(22))
                self.assertEqual((self.thread_reads, len(self.provider.snapshot_errors)), (reads, ENRICH_ATTEMPTS + 2))
                self.provider.snapshot_errors = []
                state, done = self.state(self.waiting["source_event_id"])
                self.assertEqual(state, "ready")
                # Its own observation of the thread stays the copy it read itself, under that read's own time.
                self.assertEqual(done["conversation"]["history"], self.waiting["conversation"]["history"])
                self.assertLess(done["conversation"]["history"]["observed_at"], observed)
                self.assertEqual(self.gaps(self.waiting["source_event_id"]), [])  # its timeline, read at last
                self.settled(row, sent, observed)

    def test_a_released_waiting_notification_proves_from_the_newest_read_too(self):
        row, sent = self.sequence()
        observed = self.retained(row)[2]
        self.restart()
        self.assertEqual(self.collector.release_waiting(), [self.waiting["source_event_id"]])
        state, done = self.state(self.waiting["source_event_id"])
        self.assertEqual((state, done["conversation"]["history"]), ("ready", self.waiting["conversation"]["history"]))
        self.assertEqual(self.gaps(self.waiting["source_event_id"]), [
            {"scope": "notification", "code": "addressed_action_unverified", "need": "thread_record", "error": "provider_http_502"},
            {"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["thread_record"]}])
        self.settled(row, sent, observed)

    def test_each_later_read_tells_the_waiting_notification_of_the_newest_one(self):
        row, sent = self.sequence()
        first = self.record_of(self.waiting)["superseded"]
        self.restart()  # the mark is in the waiting record: no process memory, no timer
        self.assertTrue(self.notify(22))
        second = self.record_of(self.waiting)["superseded"]
        self.assertEqual(second["holder"], self.envelope(22)["source_event_id"])
        self.assertNotEqual(second["holder"], first["holder"])
        self.assertGreater(second["at"], first["at"])
        self.assertTrue(self.retry(22))
        self.assertEqual(self.state(self.waiting["source_event_id"])[0], "ready")
        self.assertEqual(len(self.mention_events()), 2)

    def test_a_read_of_the_thread_still_open_is_made_even_when_another_notification_read_it_since(self):
        """A's notification points to a comment its read does not show yet; another reader's pass reads the thread,
        still without it. A reads the thread again all the same; while that fails it proves from the other read."""
        pointer = {"latest_comment_url": "https://api.github.com/repos/example/project/issues/comments/990"}
        self.comment(body=WITHOUT)
        self.assertTrue(self.notify(22, **pointer))
        waiting = self.waiting_event()
        self.assertEqual(self.record_of(waiting)["needs"], [{"need": "history", "code": "latest_comment_not_yet_visible"}])
        self.assertTrue(self.notify(11))
        self.assertTrue(self.record_of(waiting)["superseded"])
        late = {"id": 990, "node_id": "IC_990", "body": "@contributor one more thing", "user": OTHER, "created_at": self.written,
                "updated_at": self.written, "issue_url": ISSUE_URL}
        self.provider.rows.append(late)  # GitHub shows it now
        self.restart()
        reads = self.thread_reads
        self.provider.snapshot_errors = [unavailable()]
        self.assertFalse(self.retry(22))  # its own read fails: the other notification's read still lacks the comment
        self.assertEqual((self.thread_reads, self.mention_events()), (reads + 1, []))
        self.assertEqual(self.record_of(waiting)["needs"], [{"need": "history", "code": "latest_comment_not_yet_visible"}])
        self.assertTrue(self.retry(22))
        self.assertEqual(self.thread_reads, reads + 2)
        item, = self.mention_events()
        self.assertEqual((item["message"]["raw"]["body"], item["actor"]["platform_actor_id"]), ("@contributor one more thing", "33"))
        done = self.state(waiting["source_event_id"])[1]
        self.assertEqual((self.gaps(waiting["source_event_id"]), done["message"]["fact_evidence"]["items"]), ([], [item["source_event_id"]]))
        self.assertIn(990, [r.get("id") for r in done["conversation"]["history"]["rows"]])  # its own, newest read

    def test_a_notification_that_ends_unproven_leaves_its_readers_next_update_the_words_it_read(self):
        """A waits for an unrelated read; a mention is added after A's read and this reader's next update B reads
        it but cannot read its edit record yet. A, proving from B's read, fails on that record through its last
        attempt. What A keeps of the thread is still its own copy, so B finds the mention new against it."""
        row = self.comment(body=WITHOUT)
        self.assertTrue(self.notify(22))
        self.provider.records[("thread", 2)] = [unavailable()]
        self.assertTrue(self.notify(22))
        first = self.waiting_event()
        self.provider.records[("thread", 2)] = {"events": []}
        row = self.edit(row, WITH, MAINTAINER)
        self.provider.records[("node", "IC_980")] = [unavailable()] * (ENRICH_ATTEMPTS + 2)
        self.assertTrue(self.notify(22))
        second = [e for e in self.events() if e["_state"] == "enriching"][-1]
        self.assertEqual(self.record_of(first)["superseded"]["holder"], second["source_event_id"])
        self.restart()
        self.assertEqual(self.retry_until_done(first), ENRICH_ATTEMPTS - 1)
        self.assertEqual(self.mention_events(), [])
        self.assertEqual(self.gaps(first["source_event_id"]), [
            {"scope": "notification", "code": "addressed_action_unverified", "need": "edits:IC_980", "error": "provider_http_502"}])
        done = self.state(first["source_event_id"])[1]
        self.assertEqual(done["conversation"]["history"], first["conversation"]["history"])
        self.assertNotIn(WITH, [r.get("body") for r in done["conversation"]["history"]["rows"]])
        self.provider.records[("node", "IC_980")] = {"edits": list(self.edits)}
        self.assertTrue(self.retry(22))  # B, tried again: the mention is new against what A read, and its record answers
        item, = self.mention_events()
        self.assertEqual((item["message"]["action"], item["actor"]["platform_actor_id"], item["message"]["raw"]["body"]),
                         ("edited", "55", WITH))
        self.assertEqual(self.state(second["source_event_id"])[1]["message"]["fact_evidence"]["items"], [item["source_event_id"]])

    def test_the_mark_outlives_any_time_and_restart_until_the_waiting_notification_holds_the_newest_read(self):
        """Nothing of it expires: a companion that was down for days still knows its copy was superseded."""
        row, sent = self.sequence()
        observed = self.retained(row)[2]
        with self.store.transaction() as db:  # ten days pass with no companion running
            db.execute("UPDATE jobs SET due=due-864000")
            db.execute("UPDATE meta SET value=json_set(value,'$.first_at',json_extract(value,'$.first_at')-864000) "
                       "WHERE key LIKE 'enrich:%'")
        self.restart()
        for reader in (11, 22):  # other passes run first after the restart
            self.assertTrue(self.notify(reader))
        self.assertTrue(self.record_of(self.waiting)["superseded"])
        self.assertEqual(len(self.mention_events()), 2)
        self.assertTrue(self.retry(22))  # past its deadline: its last attempt
        self.assertEqual(self.state(self.waiting["source_event_id"])[0], "ready")
        self.settled(row, sent, observed)

    def test_without_a_kept_newer_read_the_thread_is_read_again(self):
        row, sent = self.sequence()
        observed = self.retained(row)[2]
        self.unkeep_newest(self.waiting)
        self.restart()
        reads = self.thread_reads
        self.assertTrue(self.retry(22))
        self.assertEqual(self.thread_reads, reads + 1)
        state, done = self.state(self.waiting["source_event_id"])
        self.assertEqual(state, "ready")
        self.assertGreater(done["conversation"]["history"]["observed_at"], observed)
        self.assertIn(WITH, [r.get("body") for r in done["conversation"]["history"]["rows"]])  # its newest copy
        self.settled(row, sent, observed)

    def test_without_a_kept_newer_read_a_failed_read_proves_nothing_meanwhile(self):
        row, sent = self.sequence()
        observed = self.retained(row)[2]
        self.unkeep_newest(self.waiting)
        self.restart()
        self.provider.snapshot_errors = [unavailable()]
        self.assertFalse(self.retry(22))
        self.assertEqual(self.state(self.waiting["source_event_id"])[0], "enriching")
        self.assertEqual(self.retained(row), (2, WITH, observed))  # untouched while the newer read is retried
        self.assertTrue(self.record_of(self.waiting)["superseded"])  # and its copy is still known to be superseded
        self.assertTrue(self.retry(22))
        self.assertEqual(self.state(self.waiting["source_event_id"])[0], "ready")
        self.settled(row, sent, observed)

    def test_without_a_kept_newer_read_a_superseded_copy_is_never_proven_from(self):
        for how, failure in (("last attempt", "provider_http_502"), ("definite failure", "no_longer_visible"), ("release", None)):
            with self.subTest(how=how):
                self.reset()
                row, sent = self.sequence()
                observed = self.retained(row)[2]
                self.unkeep_newest(self.waiting)
                self.restart()
                if how == "release":
                    self.assertEqual(self.collector.release_waiting(), [self.waiting["source_event_id"]])
                elif how == "definite failure":
                    self.provider.snapshot_errors = [TransportError(failure)]
                    self.assertEqual(self.retry_until_done(self.waiting), 1)  # no retry can replace it: it ends now
                else:
                    self.provider.snapshot_errors = [unavailable()] * (ENRICH_ATTEMPTS + 2)  # the thread stays unreadable
                    self.assertEqual(self.retry_until_done(self.waiting), ENRICH_ATTEMPTS - 1)
                self.provider.snapshot_errors = []
                state, done = self.state(self.waiting["source_event_id"])
                self.assertEqual(state, "ready")
                # Its open reads end as explicit gaps: the timeline it waited for, and the thread it could not read again.
                self.assertCountEqual(self.gaps(self.waiting["source_event_id"]), [
                    {"scope": "notification", "code": "addressed_action_unverified", "need": "thread_record", "error": "provider_http_502"},
                    {"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["thread_record"]}
                    if how == "release" else
                    {"scope": "notification", "code": "addressed_action_unverified", "need": "history", "error": failure}])
                # What it went out with is its own older copy, said so by its own read time; nothing was proven from it.
                self.assertLess(done["conversation"]["history"]["observed_at"], observed)
                self.assertEqual(self.retained(row), (2, WITH, observed))
                self.settled(row, sent, observed)

    def test_a_waiting_notifications_own_observation_stays_when_no_pass_read_since(self):
        row = self.comment()
        self.assertTrue(self.notify(22))
        row = self.edit(row, WITHOUT, OTHER)
        self.provider.records[("thread", 2)] = [unavailable()]
        self.assertTrue(self.notify(22))
        waiting = self.waiting_event()
        seen = self.retained(row)
        self.assertEqual(seen[:2], (1, WITHOUT))
        self.provider.records[("thread", 2)] = {"events": []}
        row = self.edit(row, WITH, MAINTAINER)  # after its read, before its retry; no other pass reads the thread
        self.restart()
        reads = self.thread_reads
        self.assertTrue(self.retry(22))
        state, done = self.state(waiting["source_event_id"])
        self.assertEqual(state, "ready")
        self.assertEqual(done["conversation"]["history"]["observed_at"], waiting["conversation"]["history"]["observed_at"])
        self.assertEqual(self.retained(row), seen)  # its copy was the newest: kept as it was, the thread not read again
        self.assertEqual((len(self.mention_events()), self.thread_reads), (1, reads))
        self.assertTrue(self.notify(11))  # the next real read reports the mention added back, once
        again = self.mention_events()[-1]
        self.assertEqual((len(self.mention_events()), again["message"]["revision"], again["actor"]["platform_actor_id"]),
                         (2, 2, "55"))
        for reader in (22, 11):
            self.assertTrue(self.notify(reader))
        self.assertEqual(len(self.mention_events()), 2)


class NewerObservationTests(FeedCase):
    def test_an_unchanged_newer_read_moves_the_time_its_words_were_last_observed(self):
        """Edits nobody observed that leave the words as retained: the later read still counts, so the next edit
        is attributed to its own editor alone."""
        row = self.comment()
        self.assertTrue(self.notify(22))
        row = self.edit(row, WITHOUT, OTHER)
        self.assertTrue(self.notify(22))
        first = self.retained(row)
        self.assertEqual(first[:2], (1, WITHOUT))
        row = self.edit(row, WITH, OTHER)  # added and taken back between two reads
        row = self.edit(row, WITHOUT, OTHER)
        self.assertTrue(self.notify(11))
        second = self.retained(row)
        self.assertEqual(second[:2], (1, WITHOUT))
        self.assertGreater(second[2], first[2])  # the same words, observed later
        self.assertEqual(len(self.mention_events()), 1)
        row = self.edit(row, WITH, MAINTAINER)
        self.assertTrue(self.notify(22))
        again = self.mention_events()[-1]
        self.assertEqual((again["message"]["revision"], again["actor"]["platform_actor_id"]), (2, "55"))
        self.assertEqual([r["actor_id"] for r in again["message"]["fact_evidence"]["change_actor"]["records"]], ["55"])
        self.assertNotIn("actor", [gap["scope"] for gap in again["message"]["coverage_gaps"]])

    def test_a_gap_never_hides_an_unreported_mention_from_a_pass_that_can_prove_it(self):
        """Reader 11's first notification of an edit that names it cannot read the thread yet; its next one reads it
        but the edit is before that window (an explicit gap). The first one, tried again, still reports it."""
        both = "@contributor and @service-writer please look"
        row = self.comment()
        self.assertTrue(self.notify(22))
        row = self.edit(row, both, MAINTAINER)
        first = self.poll(11)
        self.provider.snapshot_errors = [unavailable()]
        self.assertFalse(self.run_job(first))  # the thread read fails: this job is tried again later
        self.assertTrue(self.notify(11))  # the next update of the same notification runs first
        later = self.envelope(11)
        self.assertIn({"scope": "notification", "code": "mention_revision_unproven", "message_id": "IC_980"},
                      later["message"]["coverage_gaps"])
        self.assertEqual(self.retained(row)[:2], (1, WITH))  # the unreported name is not taken for reported
        self.assertEqual(len(self.mention_events()), 1)
        self.restart()
        self.assertTrue(self.run_job(first))
        again = self.mention_events()[-1]
        self.assertEqual(len(self.mention_events()), 2)
        self.assertEqual((again["message"]["revision"], again["actor"]["platform_actor_id"], again["facts"]), (2, "55", ["mention"]))
        self.assertEqual([span["user_id"] for span in again["message"]["fact_evidence"]["mention"]], [11])
        self.assertEqual(self.retained(row)[:2], (2, both))
        for reader in (11, 22):
            self.assertTrue(self.notify(reader))
        self.assertEqual(len(self.mention_events()), 2)


class WithdrawnMentionTests(FeedCase):
    """What only an older copy of a thread showed, and its own read was still being retried for, is never reported
    once a later read no longer shows it: an explicit gap, and the name is new again when added back."""
    GAP = {"scope": "notification", "code": "mention_withdrawn_unreported", "message_id": "IC_980"}

    def unreported(self):
        """A comment reader 22 has seen without a mention; a maintainer's edit adds one, but the edit's own record
        cannot be read, so A waits; the mention is withdrawn and reader 11's pass reads the thread without it.
        Nothing was ever reported, so the store keeps nothing of the message."""
        row = self.comment(body=WITHOUT)
        self.assertTrue(self.notify(22))
        self.assertEqual(self.mention_events(), [])
        row = self.edit(row, WITH, MAINTAINER)
        self.provider.records[("node", "IC_980")] = [unavailable()]
        self.assertTrue(self.notify(22))
        waiting = self.waiting_event()
        self.assertEqual([n["need"] for n in self.record_of(waiting)["needs"]], ["edits:IC_980"])
        row = self.edit(row, WITHOUT, OTHER)  # withdrawn before anyone could say who added it; the record answers now
        self.assertTrue(self.notify(11))
        self.assertEqual(self.mention_events(), [])
        self.assertIsNone(self.store.one("SELECT key FROM snapshots WHERE key LIKE '%:outside:%IC_980'"))
        return row, waiting

    def finish(self, waiting, how):
        if how == "release":
            self.collector.release_waiting()
            return
        if how != "thread readable":
            self.provider.snapshot_errors = [unavailable()] * (ENRICH_ATTEMPTS + 2)
        self.retry_until_done(waiting)
        self.provider.snapshot_errors = []

    def test_a_first_report_is_never_made_from_a_superseded_copy_whatever_the_thread_read_does(self):
        outcomes = []
        for how in ("thread readable", "thread unreadable to the last attempt", "release"):
            with self.subTest(how=how):
                self.reset()
                row, waiting = self.unreported()
                self.restart()
                self.finish(waiting, how)
                self.assertEqual(self.state(waiting["source_event_id"])[0], "ready")
                self.assertEqual(self.mention_events(), [])  # nothing reported from the superseded copy
                self.assertEqual(self.gaps(waiting["source_event_id"]), [self.GAP])
                self.assertIsNone(self.store.one("SELECT key FROM snapshots WHERE key LIKE '%:outside:%IC_980'"))
                outcomes.append(dump(self.state(waiting["source_event_id"])[1]["message"]["fact_evidence"]["items"]))
                row = self.edit(row, WITH, MAINTAINER)  # added back: new again, reported once with its own editor
                for reader in (22, 11, 22):
                    self.assertTrue(self.notify(reader))
                item, = self.mention_events()
                self.assertEqual((item["message"]["revision"], item["message"]["action"], item["actor"]["platform_actor_id"]),
                                 (1, "edited", "55"))
                self.assertEqual(item["message"]["raw"]["body"], WITH)
        self.assertEqual(outcomes, ["[]"] * 3)

    def test_a_gap_found_on_an_attempt_that_does_not_complete_stays_with_the_notification(self):
        row = self.comment(body=WITHOUT)
        self.assertTrue(self.notify(22))
        row = self.edit(row, WITH, MAINTAINER)
        self.provider.records[("node", "IC_980")] = [unavailable()]
        self.provider.records[("thread", 2)] = [unavailable()]  # and its timeline cannot be read either
        self.assertTrue(self.notify(22))
        waiting = self.waiting_event()
        self.assertEqual([n["need"] for n in self.record_of(waiting)["needs"]], ["edits:IC_980", "thread_record"])
        row = self.edit(row, WITHOUT, OTHER)
        self.provider.records[("thread", 2)] = {"events": []}
        self.assertTrue(self.notify(11))
        self.restart()
        self.provider.records[("thread", 2)] = [unavailable()]
        self.assertFalse(self.retry(22))  # the withdrawal is found now; the timeline still fails, so it waits on
        record = self.record_of(waiting)
        self.assertEqual(([n["need"] for n in record["needs"]], record["kwargs"]["lost"]), (["thread_record"], [self.GAP]))
        self.provider.records[("thread", 2)] = {"events": []}
        self.assertTrue(self.retry(22))
        self.assertEqual(self.gaps(waiting["source_event_id"]), [self.GAP])  # found two attempts ago, never dropped
        self.assertEqual(self.mention_events(), [])

    def test_its_own_later_read_of_the_thread_accounts_for_what_only_its_older_copy_showed(self):
        """No other notification reads the thread: the waiting one reads it again itself (the comment its
        notification points to was not visible), and the mention it was still reading for is gone by then."""
        pointer = {"latest_comment_url": "https://api.github.com/repos/example/project/issues/comments/990"}
        row = self.comment(body=WITHOUT)
        self.assertTrue(self.notify(22))
        row = self.edit(row, WITH, MAINTAINER)
        self.provider.records[("node", "IC_980")] = [unavailable()]
        self.assertTrue(self.notify(22, **pointer))
        waiting = self.waiting_event()
        self.assertEqual([n["need"] for n in self.record_of(waiting)["needs"]], ["edits:IC_980", "history"])
        row = self.edit(row, WITHOUT, OTHER)
        late = {"id": 990, "node_id": "IC_990", "body": "@contributor one more thing", "user": OTHER, "created_at": between_reads(),
                "updated_at": None, "issue_url": ISSUE_URL}
        self.provider.rows.append({**late, "updated_at": late["created_at"]})
        self.restart()
        self.assertNotIn("superseded", self.record_of(waiting))
        self.assertTrue(self.retry(22))
        item, = self.mention_events()  # the new comment, from its own later read
        self.assertEqual(item["message"]["raw"]["body"], "@contributor one more thing")
        self.assertEqual(self.gaps(waiting["source_event_id"]), [self.GAP])  # and the withdrawn mention is not dropped silently
        self.assertIsNone(self.store.one("SELECT key FROM snapshots WHERE key LIKE '%:outside:%IC_980'"))

    def test_without_a_kept_newer_read_what_only_the_superseded_copy_showed_is_still_never_reported(self):
        for how in ("thread unreadable to the last attempt", "release"):
            with self.subTest(how=how):
                self.reset()
                row, waiting = self.unreported()
                self.unkeep_newest(waiting)
                self.restart()
                self.finish(waiting, how)
                self.assertEqual(self.state(waiting["source_event_id"])[0], "ready")
                self.assertEqual(self.mention_events(), [])
                self.assertCountEqual(self.gaps(waiting["source_event_id"]), [
                    {"scope": "notification", "code": "addressed_action_unverified", "need": "edits:IC_980", "error": "provider_http_502"},
                    {"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["edits:IC_980"]}
                    if how == "release" else
                    {"scope": "notification", "code": "addressed_action_unverified", "need": "history", "error": "provider_http_502"}])
                self.assertIsNone(self.store.one("SELECT key FROM snapshots WHERE key LIKE '%:outside:%IC_980'"))

    def test_a_tracked_message_whose_second_name_was_withdrawn_gets_the_gap_whatever_the_thread_read_does(self):
        both = "@contributor and @service-writer please look"
        for how in ("thread unreadable to the last attempt", "release"):
            with self.subTest(how=how):
                self.reset()
                row = self.comment()
                self.assertTrue(self.notify(22))
                sent, = self.mention_events()
                row = self.edit(row, both, MAINTAINER)
                self.provider.records[("node", "IC_980")] = [unavailable()]
                self.assertTrue(self.notify(22))
                waiting = self.waiting_event()
                row = self.edit(row, WITH, OTHER)  # the second name is withdrawn
                self.assertTrue(self.notify(11))
                kept = self.retained(row)
                self.assertEqual(kept[:2], (1, WITH))
                self.restart()
                self.finish(waiting, how)
                self.assertEqual(self.state(waiting["source_event_id"])[0], "ready")
                self.assertEqual(self.gaps(waiting["source_event_id"]), [self.GAP])
                self.assertEqual(self.retained(row), kept)
                self.assertEqual([dump(e) for e in self.mention_events()], [dump(sent)])


class KeptRecordTests(FeedCase):
    """A waiting notification that already read a message's edit record and still waits for an account: what it
    read stays for words the newest read shows as its copy did, and goes with words that read no longer shows."""
    STRANGER = {"login": "lookup-user", "type": "User"}  # no number in the record: one more read by login
    GAP = {"scope": "notification", "code": "mention_withdrawn_unreported", "message_id": "IC_980"}

    def waiting_for_the_editor(self):
        """Reader 22 has seen the comment without a mention; an edit adds one. Its record is read; the editor's
        account is not."""
        row = self.comment(body=WITHOUT)
        self.assertTrue(self.notify(22))
        row = self.edit(row, WITH, self.STRANGER)
        self.provider.pages[LOOKUP] = unavailable(503)
        self.assertTrue(self.notify(22))
        waiting = self.waiting_event()
        record = self.record_of(waiting)
        self.assertEqual([n["need"] for n in record["needs"]], ["editor:IC_980"])
        self.assertIn("edits:IC_980", record["kwargs"]["records"])
        self.assertEqual(self.mention_events(), [])
        return row, waiting

    def record_reads(self):
        return len([call for call in self.provider.record_calls if call[0] == ("node", "IC_980")])

    def test_with_its_record_read_a_withdrawn_mention_is_still_never_reported(self):
        for how in ("account never found", "release"):
            with self.subTest(how=how):
                self.reset()
                row, waiting = self.waiting_for_the_editor()
                row = self.edit(row, WITHOUT, OTHER)
                self.assertTrue(self.notify(11))
                self.restart()
                if how == "release":
                    self.collector.release_waiting()
                else:
                    self.assertEqual(self.retry_until_done(waiting), 1)  # nothing of it is left to wait for
                self.assertEqual(self.mention_events(), [])
                self.assertEqual(self.gaps(waiting["source_event_id"]), [self.GAP])
                self.assertIsNone(self.store.one("SELECT key FROM snapshots WHERE key LIKE '%:outside:%IC_980'"))

    def test_with_its_record_read_an_unaffected_mention_is_reported_once_from_the_newest_read(self):
        for how in ("account found", "account never found", "release"):
            with self.subTest(how=how):
                self.reset()
                row, waiting = self.waiting_for_the_editor()
                self.assertTrue(self.notify(22))  # this reader's next update reads the thread: the comment is as it was
                newest = self.envelope(22)["conversation"]["history"]["observed_at"]
                self.assertEqual(self.record_of(waiting)["superseded"]["at"], newest)
                self.restart()
                reads, records = self.thread_reads, self.record_reads()
                if how == "release":
                    self.collector.release_waiting()
                elif how == "account found":
                    self.provider.pages[LOOKUP] = Response(200, {"id": 77, "login": "lookup-user", "type": "User"}, {})
                    self.assertEqual(self.retry_until_done(waiting), 1)
                else:
                    self.assertEqual(self.retry_until_done(waiting), ENRICH_ATTEMPTS - 1)
                # Neither the thread nor the record it had read for these same words was read again.
                self.assertEqual((self.thread_reads, self.record_reads()), (reads, records))
                item, = self.mention_events()
                self.assertEqual((item["message"]["action"], item["message"]["raw"]["body"]), ("edited", WITH))
                self.assertEqual(item["conversation"]["history"]["observed_at"], newest)
                if how == "account found":
                    self.assertEqual(item["actor"]["platform_actor_id"], "77")
                    self.assertEqual(self.gaps(item["source_event_id"]), [])
                else:
                    self.assertEqual(item["actor"], {"platform": "github", "attribution": "unknown"})
                    self.assertIn({"scope": "actor", "code": "change_actor_unresolved"}, self.gaps(item["source_event_id"]))
                self.assertEqual(self.state(waiting["source_event_id"])[1]["message"]["fact_evidence"]["items"],
                                 [item["source_event_id"]])
                for reader in (22, 11):
                    self.assertTrue(self.notify(reader))
                self.assertEqual([dump(e) for e in self.mention_events()], [dump(item)])  # once, never rewritten

    def test_the_record_of_a_message_the_newest_read_shows_with_other_words_is_read_again(self):
        row, waiting = self.waiting_for_the_editor()
        row = self.edit(row, "@contributor please look today", OTHER)  # still names the reader: other words, another editor
        self.assertTrue(self.notify(22))
        self.restart()
        self.provider.pages[LOOKUP] = Response(200, {"id": 77, "login": "lookup-user", "type": "User"}, {})
        records = self.record_reads()
        self.assertEqual(self.retry_until_done(waiting), 1)
        self.assertEqual(self.record_reads(), records + 1)
        item, = self.mention_events()
        self.assertEqual(item["message"]["raw"]["body"], "@contributor please look today")
        # Both edits since this reader last saw the comment made these words: no single account is named for them.
        self.assertEqual(item["actor"], {"platform": "github", "attribution": "unknown"})
        self.assertEqual([r["actor_id"] for r in item["message"]["fact_evidence"]["change_actor"]["records"]], ["33", "77"])
        self.assertIn({"scope": "actor", "code": "multiple_change_actors"}, self.gaps(item["source_event_id"]))


class UnaffectedWordsTests(FeedCase):
    """New words only the waiting notification can report (its reader's later updates see them as old), still as
    written when the thread is read again: reported once, from the newest read, whatever the retries meet."""

    def waiting_for_the_author(self):
        row = self.comment(ident=981, user={"login": "lookup-user", "type": "User"})
        self.provider.pages[LOOKUP] = unavailable(503)
        self.assertTrue(self.notify(22))
        waiting = self.waiting_event()
        self.assertEqual([n["need"] for n in self.record_of(waiting)["needs"]], ["author:IC_981"])
        self.assertTrue(self.notify(22))  # this reader's next update: written before its window, nothing new in it
        self.assertEqual(self.envelope(22)["_state"], "ready")
        self.assertEqual(self.mention_events(), [])
        return row, waiting, self.envelope(22)["conversation"]["history"]["observed_at"]

    def test_new_words_as_written_are_reported_once_from_the_newest_read(self):
        for how in ("account found", "thread unreadable and account never found", "release"):
            with self.subTest(how=how):
                self.reset()
                row, waiting, newest = self.waiting_for_the_author()
                self.restart()
                reads = self.thread_reads
                if how == "release":
                    self.collector.release_waiting()
                elif how == "account found":
                    self.provider.pages[LOOKUP] = Response(200, {"id": 77, "login": "lookup-user", "type": "User"}, {})
                    self.assertEqual(self.retry_until_done(waiting), 1)
                else:
                    self.provider.snapshot_errors = [unavailable()] * (ENRICH_ATTEMPTS + 2)
                    self.assertEqual(self.retry_until_done(waiting), ENRICH_ATTEMPTS - 1)
                    self.provider.snapshot_errors = []
                self.assertEqual(self.thread_reads, reads)
                item, = self.mention_events()
                self.assertEqual((item["message"]["action"], item["message"]["raw"]["body"], item["facts"]),
                                 ("observed", WITH, ["mention"]))
                self.assertEqual(item["conversation"]["history"]["observed_at"], newest)
                self.assertNotIn("forward_origin", item["message"])  # its author's own new words
                # The thread as that read kept it does not number this author; the item's own author is read since.
                copied = {"scope": "history", "code": "history_actor_unavailable"}
                if how == "account found":
                    self.assertEqual((item["actor"]["platform_actor_id"], self.gaps(item["source_event_id"])), ("77", [copied]))
                else:
                    self.assertEqual(item["actor"]["attribution"], "unknown")
                    self.assertEqual([gap.get("need") for gap in self.gaps(item["source_event_id"]) if gap != copied],
                                     ["author:IC_981"])
                for reader in (22, 11):
                    self.assertTrue(self.notify(reader))
                self.assertEqual([dump(e) for e in self.mention_events()], [dump(item)])

    def test_what_was_already_proven_stays_as_it_was_sent(self):
        for how in ("retry", "release"):
            with self.subTest(how=how):
                self.reset()
                self.comment()
                self.provider.records[("thread", 2)] = [unavailable()]
                self.assertTrue(self.notify(22))  # the new words go out at once; the notification waits for its timeline
                waiting = self.waiting_event()
                sent, = self.mention_events()
                self.provider.records[("thread", 2)] = {"events": []}
                self.assertTrue(self.notify(11))
                self.assertTrue(self.record_of(waiting)["superseded"])
                self.restart()
                if how == "release":
                    self.collector.release_waiting()
                else:
                    self.assertEqual(self.retry_until_done(waiting), 1)
                done = self.state(waiting["source_event_id"])[1]
                self.assertEqual(done["message"]["fact_evidence"]["items"], [sent["source_event_id"]])
                self.assertEqual([dump(e) for e in self.mention_events()], [dump(sent)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
