"""One GitHub message or action is one event, attributed to what made it (synthetic accounts only).

The six seams an independent review named: an owner's edit of words whose author is unknown, an outside mention an
edit added, one comment reaching two readers, a notification update read as a revision, a failed optional read
erasing a proven mention, and an incomplete timeline read as complete; plus a host that stops reading changes.
"""
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from test_attention_repair import SIGNAL_NEW_DISCUSSION, AttentionCase, OTHER, OutsideCase, gql, issue, pull, target
from test_transport import CONFIG, HUMAN, REPO, WRITER
from gh_presence.collector import ENRICH_ATTEMPTS, ENRICH_RECORD_ATTEMPTS, OUTSIDE_TIMELINE_PAGES, Collector
from gh_presence.host import Host
from gh_presence.http import Response, TransportError
from gh_presence.store import Store, dump, now

OWNER = {"id": 44, "login": "owner-account", "type": "User"}
MAINTAINER = {"id": 55, "login": "maintainer", "type": "User"}
ISSUE_URL = "https://api.github.com/repos/example/project/issues/2"


def unavailable(status=502):
    return TransportError("provider_http_" + str(status), Response(status, {}, {}))


def stamp(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def note(ident, user, body, written, updated=None):
    return {"id": ident, "node_id": "IC_" + str(ident), "body": body, "user": user, "created_at": written,
            "updated_at": updated or written, "issue_url": ISSUE_URL}


def between_reads():
    """A provider time strictly between the connector's previous read of a thread and its next one, on any clock."""
    time.sleep(0.02)
    at = datetime.now(timezone.utc).isoformat()
    time.sleep(0.02)
    return at


def creation(at, user):
    """GitHub's oldest content edit of an edited comment: its original revision, by its author."""
    return {"edited_at": at, "deleted": False, "editor": gql(user)}


def revision(at, user):
    return {"edited_at": at, "deleted": False, "editor": gql(user)}


def core_reads_as_change(message):
    """The host's documented rule (CREATING_SKILLS, ``state_change``): a change, not new words."""
    return (any(item.get("kind") == "state_change" for item in message.get("non_text") or [])
            or bool(str(message.get("previous_source_event_id") or "").strip()))


class OwnerEditTests(AttentionCase):
    def test_an_owner_edit_of_words_whose_author_is_unknown_keeps_them_external(self):
        self.capable()
        ghost = issue(user=None)  # GitHub names no author for a deleted account
        self.provider.rows = [ghost]
        self.page("issues", [ghost])
        edited = issue(user=None, body="Rewritten by the owner", updated_at="2026-02-02T00:00:00Z")
        self.provider.records[("thread", 1)] = {"edits": [revision(edited["updated_at"], OWNER)], "events": []}
        self.provider.rows = [edited]
        value = self.page("issues", [edited])
        message = value["message"]
        self.assertEqual(value["actor"]["platform_actor_id"], "44")  # the editor made the change
        self.assertEqual(value["text"], "Question\n\nRewritten by the owner")
        self.assertEqual(message["original_author"], {"platform": "github", "attribution": "unknown"})
        # The copied words are another (here unknown) author's: never the editor's own words.
        self.assertEqual(message["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
        self.assertTrue(self.non_text(value, "change_attribution")[0].startswith(
            "Edited by @owner-account (id 44); the text was first written by an unidentified account."))

    def test_an_edit_proven_to_be_by_its_own_author_stays_their_words(self):
        self.capable()
        owned = issue(user=OWNER)
        self.provider.rows = [owned]
        self.page("issues", [owned])
        edited = issue(user=OWNER, body="Clarified by its author", updated_at="2026-02-02T00:00:00Z")
        self.provider.records[("thread", 1)] = {"edits": [revision(edited["updated_at"], OWNER)], "events": []}
        self.provider.rows = [edited]
        value = self.page("issues", [edited])
        self.assertEqual(value["actor"]["platform_actor_id"], "44")
        self.assertNotIn("forward_origin", value["message"])

    def observe(self, raw, record):
        self.provider.records[("thread", raw["number"])] = record
        self.provider.rows = [raw]
        return self.page("issues", [raw])

    def renamed(self, raw, title, at, user=OWNER):
        return ({**raw, "title": title, "updated_at": at},
                {"type": "RenamedTitleEvent", "created_at": at, "actor": gql(user), "current_title": title})

    def assert_mixed(self, value, code, text):
        """Their first author made this edit, yet the words are not that account's alone: never own words."""
        message = value["message"]
        self.assertEqual(value["actor"]["platform_actor_id"], "44")
        self.assertEqual(value["text"], text)
        self.assertEqual(message["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
        self.assertEqual(message["fact_evidence"]["word_origin"]["code"], code)
        self.assertIn("not established as theirs alone (" + code + ")", self.non_text(value, "change_attribution")[0])
        self.assertEqual(message["coverage_gaps"], [])  # who made this change is known; whose the words are is not

    def test_a_title_edit_by_the_author_never_returns_another_accounts_body_as_their_words(self):
        """Owner opens, another account edits the body, the owner then changes only the title."""
        collaborator = "2026-02-02T00:00:00Z"
        for form in ("real record", "record without the earlier edit", "edit made before first sight"):
            with self.subTest(form=form):
                self.reset()
                self.capable()
                opened = issue(user=OWNER)
                foreign = issue(user=OWNER, body="Changed by a collaborator", updated_at=collaborator)
                edits = [revision(collaborator, OTHER), creation(opened["created_at"], OWNER)]
                if form == "edit made before first sight":
                    self.observe(foreign, {"events": []})  # first seen with the collaborator's words already in it
                else:
                    self.observe(opened, {"events": []})
                    seen = self.observe(foreign, {"edits": edits, "events": []})
                    self.assertEqual(seen["actor"]["platform_actor_id"], "33")
                    self.assertEqual(seen["message"]["forward_origin"]["sender_user"]["id"], "44")
                raw, event = self.renamed(foreign, "Question, retitled", "2026-02-03T00:00:00Z")
                # GitHub's real record still lists the collaborator's edit; a narrower one must not change the answer.
                record = {"events": [event], **({} if form == "record without the earlier edit" else {"edits": edits})}
                value = self.observe(raw, record)
                self.assert_mixed(value, "other_editor_recorded" if form == "edit made before first sight"
                                  else "earlier_revision_not_its_authors", "Question, retitled\n\nChanged by a collaborator")
                self.assertEqual(self.non_text(value), ['Changed the title of the issue from "Question" to "Question, retitled".'])

    def test_the_authors_own_successive_edits_stay_their_words(self):
        self.capable()
        opened = issue(user=OWNER)
        self.observe(opened, {"events": []})
        edits = [creation(opened["created_at"], OWNER)]
        first = issue(user=OWNER, body="Clarified by its author", updated_at="2026-02-02T00:00:00Z")
        edits.insert(0, revision(first["updated_at"], OWNER))
        steps = [self.observe(first, {"edits": list(edits), "events": []})]
        # Another account closes it: a change of state leaves nobody else's words in the text.
        closed = {**first, "state": "closed", "updated_at": "2026-02-03T00:00:00Z"}
        other = self.observe(closed, {"edits": list(edits), "events": [
            {"type": "ClosedEvent", "created_at": closed["updated_at"], "actor": gql(OTHER)}]})
        self.assertEqual((other["actor"]["platform_actor_id"], other["text"]), ("33", ""))
        retitled, event = self.renamed(closed, "Question, retitled", "2026-02-04T00:00:00Z")
        steps.append(self.observe(retitled, {"edits": list(edits), "events": [event]}))
        again = {**retitled, "body": "Clarified once more", "updated_at": "2026-02-05T00:00:00Z"}
        edits.insert(0, revision(again["updated_at"], OWNER))
        steps.append(self.observe(again, {"edits": list(edits), "events": []}))
        for value in steps:
            self.assertEqual(value["actor"]["platform_actor_id"], "44")
            self.assertNotIn("forward_origin", value["message"])
            self.assertNotIn("word_origin", value["message"]["fact_evidence"])
            self.assertEqual(self.non_text(value, "change_attribution"),
                             ["GitHub records @owner-account (id 44) as having made this change."])
        self.assertEqual(steps[-1]["text"], "Question, retitled\n\nClarified once more")
        self.assertNotIn("foreign_words", json.loads(self.store.one(
            "SELECT value FROM snapshots WHERE key LIKE '%:issue:101'")["value"]))

    def test_another_accounts_title_stays_in_the_words_the_author_edits_later(self):
        self.capable()
        opened = issue(user=OWNER)
        self.observe(opened, {"events": []})
        retitled, event = self.renamed(opened, "Renamed by a collaborator", "2026-02-02T00:00:00Z", OTHER)
        self.assertEqual(self.observe(retitled, {"events": [event]})["actor"]["platform_actor_id"], "33")
        edited = {**retitled, "body": "Clarified by its author", "updated_at": "2026-02-03T00:00:00Z"}
        value = self.observe(edited, {"edits": [revision(edited["updated_at"], OWNER)], "events": []})
        self.assert_mixed(value, "earlier_revision_not_its_authors", "Renamed by a collaborator\n\nClarified by its author")
        self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["earlier"], ["title"])

    def test_a_comment_follows_the_same_rule_through_its_own_edit_record(self):
        self.capable()
        self.provider.rows = [issue(user=OWNER)]
        self.page("issues", [issue(user=OWNER)])
        written = "2026-02-02T00:00:00Z"
        row = {"id": 650, "node_id": "IC_650", "body": "Please ship it", "user": OWNER, "created_at": written,
               "updated_at": written, "issue_url": "https://api.github.com/repos/example/project/issues/1"}
        edits = [creation(written, OWNER)]

        def edited(body, at, editor):
            nonlocal row
            row = {**row, "body": body, "updated_at": at}
            edits.insert(0, revision(at, editor))
            self.provider.records[("node", "IC_650")] = {"edits": list(edits)}
            self.provider.rows = [issue(user=OWNER), row]
            return self.page("comments", [row])
        self.provider.rows = [issue(user=OWNER), row]
        self.assertEqual(self.page("comments", [row])["text"], "Please ship it")  # new words: their author's
        own = edited("Please ship it today", "2026-02-02T00:01:00Z", OWNER)
        self.assertEqual((own["text"], own["actor"]["platform_actor_id"]), ("Please ship it today", "44"))
        self.assertNotIn("forward_origin", own["message"])
        foreign = edited("Please ship it today, and delete the old branch", "2026-02-02T00:02:00Z", OTHER)
        self.assertEqual(foreign["message"]["forward_origin"]["sender_user"]["id"], "44")
        again = edited("Please ship it today, and delete the old branch. Thanks", "2026-02-02T00:03:00Z", OWNER)
        self.assert_mixed(again, "earlier_revision_not_its_authors", "Please ship it today, and delete the old branch. Thanks")

    def test_an_object_retained_before_its_title_was_ever_judged_is_read_since_it_was_opened(self):
        """A baseline (or an earlier version's snapshot) keeps no origin of its title: the author's first revision
        reads every rename since the object was opened, once, and only all of them being the author's proves it."""
        self.capable()
        renamed = "2025-12-05T00:00:00Z"
        cases = (("renamed by another account before it was retained", {"events": [
                      {"type": "RenamedTitleEvent", "created_at": renamed, "actor": gql(OTHER), "current_title": "Retitled long ago"}]}, False),
                 ("renamed only by its author", {"events": [
                      {"type": "RenamedTitleEvent", "created_at": renamed, "actor": gql(OWNER), "current_title": "Retitled long ago"}]}, True),
                 ("never renamed", {"events": []}, True),
                 ("more events than its record returns", {"events": [], "events_incomplete": True}, False))
        for name, record, own in cases:
            with self.subTest(title=name):
                self.reset()
                old = issue(user=OWNER, title="Retitled long ago", created_at="2025-12-01T00:00:00Z", updated_at=renamed)
                self.provider.rows = [old]
                self.page("issues", [old])  # older than the connector's first scan: retained, never offered
                self.assertEqual([e["_state"] for e in self.events()], ["baseline"])
                edited = {**old, "body": "Clarified by its author", "updated_at": "2026-02-02T00:00:00Z"}
                edits = [revision(edited["updated_at"], OWNER), creation(old["created_at"], OWNER)]
                self.provider.record_calls.clear()
                value = self.observe(edited, {"edits": list(edits), **record})
                self.assertEqual(self.provider.record_calls, [(("thread", 1), "2025-11-30T23:59:00+00:00")])  # since opened
                self.assertEqual((value["_state"], value["actor"]["platform_actor_id"]), ("ready", "44"))
                if own:
                    self.assertNotIn("forward_origin", value["message"])
                    self.assertEqual(value["text"], "Retitled long ago\n\nClarified by its author")
                else:
                    self.assert_mixed(value, "title_not_its_authors", "Retitled long ago\n\nClarified by its author")
                kept = json.loads(self.store.one("SELECT value FROM snapshots WHERE key LIKE '%:issue:101'")["value"])
                self.assertEqual((kept.get("title_read"), kept.get("foreign_words")), (True, None if own else {"title": True}))
                # From here on its title is judged as it changes: the next revision reads only its own interval.
                again = {**edited, "body": "Clarified once more", "updated_at": "2026-02-03T00:00:00Z"}
                edits.insert(0, revision(again["updated_at"], OWNER))
                self.provider.record_calls.clear()
                value = self.observe(again, {"edits": list(edits), "events": []})
                self.assertEqual(self.provider.record_calls, [(("thread", 1), "2026-02-01T23:59:00+00:00")])
                self.assertEqual("forward_origin" not in value["message"], own)

    def test_an_incomplete_edit_record_never_proves_the_whole_text(self):
        self.capable()
        opened = issue(user=OWNER)
        self.observe(opened, {"events": []})
        edited = issue(user=OWNER, body="Edited for the fifty-first time", updated_at="2026-02-02T00:00:00Z")
        record = {"edits": [revision(edited["updated_at"], OWNER), revision("2026-01-20T00:00:00Z", OWNER)],
                  "edits_incomplete": True, "events": []}
        self.assert_mixed(self.observe(edited, record), "edit_record_incomplete", "Question\n\nEdited for the fifty-first time")

    def run_enrichment(self):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0 WHERE stream='repo:321:enrich'")
        return self.tick_only("repo:321:enrich")

    def test_an_edit_githubs_record_answered_without_keeps_the_text_unproven(self):
        self.capable()
        opened = issue(user=OWNER)
        self.observe(opened, {"events": []})
        changed = issue(user=OWNER, body="Changed, and GitHub's record never shows by whom", updated_at="2026-02-02T00:00:00Z")
        waiting = self.observe(changed, {"edits": [], "events": []})
        self.assertEqual(waiting["_state"], "enriching")  # reread a bounded number of times
        runs = 0
        while self.latest()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_RECORD_ATTEMPTS)
            self.run_enrichment()
        unknown = self.latest()
        self.assertEqual(unknown["actor"]["attribution"], "unknown")
        self.assertIn({"scope": "actor", "code": "change_record_missing"}, unknown["message"]["coverage_gaps"])
        raw, event = self.renamed(changed, "Question, retitled", "2026-02-03T00:00:00Z")
        # The record that already answered without that edit cannot prove the text now either.
        value = self.observe(raw, {"edits": [], "events": [event]})
        self.assert_mixed(value, "earlier_revision_not_its_authors",
                          "Question, retitled\n\nChanged, and GitHub's record never shows by whom")
        self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["earlier"], ["body"])

    def test_an_earlier_revision_still_being_read_is_never_taken_for_the_authors(self):
        """The edit before this one waits for its record: until that is read, the author's next edit proves nothing
        about the whole text; once it is read, its answer stays with the object either way."""
        for editor, later in ((OTHER, "mixed"), (OWNER, "own")):
            with self.subTest(earlier_editor=editor["login"]):
                self.reset()
                self.capable()
                opened = issue(user=OWNER)
                self.observe(opened, {"events": []})
                changed = issue(user=OWNER, body="Changed while GitHub was failing", updated_at="2026-02-02T00:00:00Z")
                waiting = self.observe(changed, [unavailable()])
                self.assertEqual(waiting["_state"], "enriching")
                raw, event = self.renamed(changed, "Question, retitled", "2026-02-03T00:00:00Z")
                meanwhile = self.observe(raw, {"events": [event]})  # read at once; the edit before it is not
                self.assert_mixed(meanwhile, "earlier_revision_unsettled", "Question, retitled\n\nChanged while GitHub was failing")
                edits = [revision(changed["updated_at"], editor), creation(opened["created_at"], OWNER)]
                self.provider.records[("thread", 1)] = {"edits": edits, "events": []}
                self.assertTrue(self.run_enrichment())
                settled = next(e for e in self.events() if e["source_event_id"] == waiting["source_event_id"])
                self.assertEqual(settled["actor"]["platform_actor_id"], str(editor["id"]))
                again = {**raw, "body": "Clarified by its author", "updated_at": "2026-02-04T00:00:00Z"}
                # A record that lists only this latest edit: the earlier answer must already be with the object.
                value = self.observe(again, {"edits": [revision(again["updated_at"], OWNER)], "events": []})
                if later == "mixed":
                    self.assert_mixed(value, "earlier_revision_not_its_authors", "Question, retitled\n\nClarified by its author")
                else:  # the earlier edit proved to be the author's own: nothing stays against their words
                    self.assertEqual(value["actor"]["platform_actor_id"], "44")
                    self.assertNotIn("forward_origin", value["message"])

    def test_every_rename_between_two_polls_is_judged_not_only_the_one_that_set_the_title(self):
        """GitHub's timeline keeps each rename: one another account made before the author's own later rename in the
        same interval still leaves its words in the title, now and in every later revision."""
        first, then, latest = "2026-02-02T00:00:00Z", "2026-02-02T00:05:00Z", "2026-02-02T00:10:00Z"

        def rename(at, user, title):
            return {"type": "RenamedTitleEvent", "created_at": at, "actor": gql(user), "current_title": title}
        owned = {"id": 1, "type": "AssignedEvent", "created_at": "2026-02-01T00:00:00Z", "actor": gql(OWNER),
                 "assignee": target(OWNER)}  # at the interval's start: the page reaches back over all of it
        cases = (("another account, then the author", [rename(first, OTHER, "Renamed by a collaborator"),
                  rename(then, OWNER, "Renamed by a collaborator, clarified")], {}, "multiple_change_actors"),
                 ("the author twice", [rename(first, OWNER, "Retitled"), rename(then, OWNER, "Renamed by a collaborator, clarified")],
                  {}, None),
                 ("another account, then the author with a body edit", [rename(first, OTHER, "Renamed by a collaborator"),
                  rename(then, OWNER, "Renamed by a collaborator, clarified")], {"body": "Clarified by its author"},
                  "multiple_change_actors"),
                 ("an earlier page may hold another rename", [rename(then, OWNER, "Renamed by a collaborator, clarified")],
                  {"events_incomplete": True}, "change_record_incomplete"),
                 ("a full page back over the interval", [owned, rename(then, OWNER, "Renamed by a collaborator, clarified")],
                  {"events_incomplete": True}, None),
                 ("a later rename not returned yet", [rename(first, OWNER, "Renamed by a collaborator, clarified"),
                  rename(then, OTHER, "Renamed again")], {"returned_later": rename(latest, OTHER, "Renamed by a collaborator, clarified")},
                  "multiple_change_actors"))
        for name, events, extra, code in cases:
            with self.subTest(case=name):
                self.reset()
                self.capable()
                opened = issue(user=OWNER)
                self.observe(opened, {"events": []})
                body = extra.get("body") or opened["body"]
                at = latest if "returned_later" in extra else then
                raw = {**opened, "title": "Renamed by a collaborator, clarified", "body": body, "updated_at": at}
                edits = [revision(then, OWNER), creation(opened["created_at"], OWNER)] if "body" in extra else []
                record = {"events": events, "edits": edits, **({"events_incomplete": True} if extra.get("events_incomplete") else {})}
                value = self.observe(raw, record)
                if "returned_later" in extra:
                    # The record does not show the rename that set this title yet: read again, never judged meanwhile.
                    self.assertEqual(value["_state"], "enriching")
                    self.provider.records[("thread", 1)] = {"events": events + [extra["returned_later"]], "edits": []}
                    self.assertTrue(self.run_enrichment())
                    value = self.latest()
                    self.assertEqual(value["_state"], "ready")
                text = "Renamed by a collaborator, clarified\n\n" + body
                kept = json.loads(self.store.one("SELECT value FROM snapshots WHERE key LIKE '%:issue:101'")["value"])
                if code is None:  # every rename of the interval is the author's: their words
                    self.assertEqual((value["actor"]["platform_actor_id"], value["text"]), ("44", text))
                    self.assertNotIn("forward_origin", value["message"])
                    self.assertNotIn("foreign_words", kept)
                else:
                    self.assertEqual(value["actor"], {"platform": "github", "attribution": "unknown"})
                    self.assertEqual(value["text"], text)
                    # The revised words copy their first author's text with another hand in them: never own words.
                    self.assertEqual(value["message"]["forward_origin"]["sender_user"]["id"], "44")
                    self.assertIn({"scope": "actor", "code": code}, value["message"]["coverage_gaps"])
                    self.assertEqual(kept["foreign_words"], {"title": True})
                again = {**raw, "body": "Clarified once more", "updated_at": "2026-02-03T00:00:00Z"}
                later = self.observe(again, {"edits": [revision(again["updated_at"], OWNER)], "events": []})
                if code is None:
                    self.assertNotIn("forward_origin", later["message"])
                else:
                    self.assert_mixed(later, "earlier_revision_not_its_authors", "Renamed by a collaborator, clarified\n\nClarified once more")
                    self.assertEqual(later["message"]["fact_evidence"]["word_origin"]["earlier"], ["title"])

    def dispatched(self):
        """What the real dispatch offers the host now, in order (the host answers silently)."""
        sent = []
        while True:
            count = len(sent)
            host = Host(SimpleNamespace(request=lambda method, path, payload: sent.append(payload) or Response(
                200, {"status": "completed", "outcome": "silent"}, {})), self.store, self.outbox)
            host.dispatch()
            host.close()
            if len(sent) == count:
                return [payload["event"] for payload in sent]

    def restart(self):
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.outbox.store = self.store
        self.collector = Collector(self.store, self.provider, CONFIG)

    def test_a_revision_still_being_read_holds_every_later_revision_until_it_is_read(self):
        """Another account renames the owner's issue while GitHub fails; two and more revisions are observed before
        that read succeeds, across a restart. None of them takes the words for the author's alone; the events go
        out in order once the earlier one is read; afterwards its answer decides, either way."""
        for editor in (OTHER, OWNER):
            with self.subTest(renamed_by=editor["login"]):
                self.reset()
                self.capable()
                opened = issue(user=OWNER)
                first = self.observe(opened, {"events": []})
                renamed = {**opened, "title": "Renamed by a collaborator", "updated_at": "2026-02-02T00:00:00Z"}
                waiting = self.observe(renamed, [unavailable()])
                self.assertEqual(waiting["_state"], "enriching")
                closed = {**renamed, "state": "closed", "updated_at": "2026-02-03T00:00:00Z"}
                closing = {"type": "ClosedEvent", "created_at": closed["updated_at"], "actor": gql(OTHER)}
                close = self.observe(closed, {"events": [closing]})
                self.assertEqual(close["actor"]["platform_actor_id"], "33")
                self.restart()
                edits = [creation(opened["created_at"], OWNER)]
                held = []
                for n, body in enumerate(("Clarified by its author", "Clarified once more")):
                    raw = {**closed, "body": body, "updated_at": "2026-02-0%dT00:00:00Z" % (4 + n)}
                    edits.insert(0, revision(raw["updated_at"], OWNER))
                    value = self.observe(raw, {"edits": list(edits), "events": []})
                    self.assert_mixed(value, "earlier_revision_unsettled", "Renamed by a collaborator\n\n" + body)
                    held.append(value["source_event_id"])
                    if not n:
                        self.restart()
                # Only the opening goes out meanwhile: every later change waits behind the read still open.
                self.assertEqual([e["source_event_id"] for e in self.dispatched()], [first["source_event_id"]])
                self.provider.records[("thread", 1)] = {"events": [
                    {"type": "RenamedTitleEvent", "created_at": renamed["updated_at"], "actor": gql(editor),
                     "current_title": renamed["title"]}, closing], "edits": list(edits)}
                self.assertTrue(self.run_enrichment())
                sent = self.dispatched()
                self.assertEqual([e["source_event_id"] for e in sent],
                                 [waiting["source_event_id"], close["source_event_id"]] + held)
                self.assertEqual(sent[0]["actor"]["platform_actor_id"], str(editor["id"]))
                for event in sent[2:]:  # as built: never rewritten by the later answer
                    self.assertEqual(event["message"]["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
                    self.assertEqual(event["message"]["fact_evidence"]["word_origin"]["code"], "earlier_revision_unsettled")
                again = {**closed, "body": "Clarified a third time", "updated_at": "2026-02-06T00:00:00Z"}
                edits.insert(0, revision(again["updated_at"], OWNER))
                value = self.observe(again, {"edits": list(edits), "events": []})
                if editor is OTHER:
                    self.assert_mixed(value, "earlier_revision_not_its_authors", "Renamed by a collaborator\n\nClarified a third time")
                else:  # the author's own rename after all: their later words are theirs again
                    self.assertEqual(value["actor"]["platform_actor_id"], "44")
                    self.assertNotIn("forward_origin", value["message"])

    def test_a_first_sight_still_being_read_holds_every_later_revision_until_it_is_read(self):
        """The owner's issue another account renamed before it was first seen, its record failing: a state change and
        the owner's edit after it never take the title for the owner's words while that read is open."""
        self.capable()
        raw = issue(user=OWNER, title="Renamed before first sight", updated_at="2026-02-01T06:00:00Z")
        waiting = self.observe(raw, [unavailable()])
        self.assertEqual(waiting["_state"], "enriching")
        closed = {**raw, "state": "closed", "updated_at": "2026-02-02T00:00:00Z"}
        closing = {"type": "ClosedEvent", "created_at": closed["updated_at"], "actor": gql(OTHER)}
        self.observe(closed, {"events": [closing]})
        edited = {**closed, "body": "Clarified by its author", "updated_at": "2026-02-03T00:00:00Z"}
        edits = [revision(edited["updated_at"], OWNER), creation(raw["created_at"], OWNER)]
        value = self.observe(edited, {"edits": list(edits), "events": []})
        self.assert_mixed(value, "earlier_revision_unsettled", "Renamed before first sight\n\nClarified by its author")
        self.provider.records[("thread", 1)] = {"edits": list(edits), "events": [
            {"type": "RenamedTitleEvent", "created_at": "2026-02-01T05:00:00Z", "actor": gql(OTHER), "current_title": raw["title"]},
            closing]}
        self.assertTrue(self.run_enrichment())
        first = next(e for e in self.events() if e["source_event_id"] == waiting["source_event_id"])
        self.assertEqual((first["_state"], first["message"]["fact_evidence"]["word_origin"]["code"]), ("ready", "other_editor_recorded"))
        again = {**edited, "body": "Clarified once more", "updated_at": "2026-02-04T00:00:00Z"}
        edits.insert(0, revision(again["updated_at"], OWNER))
        self.assert_mixed(self.observe(again, {"edits": list(edits), "events": []}), "earlier_revision_not_its_authors",
                          "Renamed before first sight\n\nClarified once more")

    def reset(self):
        with self.store.transaction() as db:
            db.execute("DELETE FROM snapshots")
            db.execute("DELETE FROM jobs")
            db.execute("DELETE FROM meta WHERE key LIKE 'enrich:%' OR key LIKE 'dispatch:%'")
            db.execute("DELETE FROM inbox WHERE id!='event-1'")

    def test_words_changed_before_the_host_read_changes_are_judged_by_githubs_record(self):
        opened = issue(user=OWNER)
        body = issue(user=OWNER, body="Edited by someone, unattributed here", updated_at="2026-02-02T00:00:00Z")
        for editor, own in ((OWNER, True), (OTHER, False)):
            with self.subTest(earlier_editor=editor["login"]):
                self.reset()
                self.store.set_meta("host_capabilities", {"ready": True})  # no change capability: no record is read
                self.observe(opened, {"events": []})
                legacy = self.observe(body, {"events": []})
                self.assertEqual((legacy["facts"], legacy["actor"]["attribution"]), (["any_message"], "unknown"))
                self.assertFalse(self.provider.record_calls)
                self.capable()
                again = {**body, "body": "Clarified by its author", "updated_at": "2026-02-03T00:00:00Z"}
                value = self.observe(again, {"edits": [revision(again["updated_at"], OWNER), revision(body["updated_at"], editor),
                                                       creation(opened["created_at"], OWNER)], "events": []})
                self.assertEqual(value["actor"]["platform_actor_id"], "44")
                self.assertEqual("forward_origin" not in value["message"], own)
                self.provider.record_calls.clear()

    def test_a_title_changed_while_nothing_recorded_who_changed_it_stays_unproven(self):
        opened = issue(user=OWNER)
        self.store.set_meta("host_capabilities", {"ready": True})  # no change capability
        self.observe(opened, {"events": []})
        retitled = {**opened, "title": "Retitled, unattributed here", "updated_at": "2026-02-02T00:00:00Z"}
        self.observe(retitled, {"events": []})
        self.capable()
        edited = {**retitled, "body": "Clarified by its author", "updated_at": "2026-02-03T00:00:00Z"}
        value = self.observe(edited, {"edits": [revision(edited["updated_at"], OWNER)], "events": []})
        # Only an observed interval's own title events are read: nothing proves this title later, the words stay mixed.
        self.assert_mixed(value, "earlier_revision_not_its_authors", "Retitled, unattributed here\n\nClarified by its author")


class FirstSeenWordsTests(AttentionCase):
    """New words first observed after they were written: whether they are their author's alone is GitHub's own
    record to say. The author having made the latest edit proves nothing, and an unknown record never makes them
    that account's own words. They stay new words under their author either way."""

    WRITTEN, EDITED = "2026-02-02T00:00:00Z", "2026-02-02T00:05:00Z"

    def setUp(self):
        super().setUp()
        self.provider.rows = [issue()]
        self.page("issues", [issue()])  # the thread: an issue opened by a watched account
        self.fresh()

    def fresh(self, capable=True):
        with self.store.transaction() as db:
            db.execute("DELETE FROM snapshots WHERE key NOT LIKE '%:issue:101'")
            db.execute("DELETE FROM jobs")
            db.execute("DELETE FROM meta WHERE key LIKE 'enrich:%'")
            db.execute("DELETE FROM inbox WHERE id!='event-1'")
        self.store.set_meta("host_capabilities", {"ready": True, **({"presence_change_context": 1} if capable else {})})
        self.provider.record_calls.clear()
        self.provider.users.clear()

    def note(self, ident=660, user=OWNER, body="Please ship it today", updated=None):
        return {"id": ident, "node_id": "IC_" + str(ident), "body": body, "user": user, "created_at": self.WRITTEN,
                "updated_at": updated or self.EDITED, "issue_url": "https://api.github.com/repos/example/project/issues/1"}

    def observe(self, row, record=None, kind="comments", **extra):
        if record is not None:
            self.provider.records[("node", row["node_id"])] = record
        if kind == "comments":
            self.provider.rows = [issue(), row]
        return self.page(kind, [row], **extra)

    def run_enrichment(self):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET due=0 WHERE stream='repo:321:enrich'")
        return self.tick_only("repo:321:enrich")

    def drain(self, limit=ENRICH_ATTEMPTS):
        runs = 0
        while self.latest()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, limit)
            self.run_enrichment()
        return runs

    def assert_own(self, value, author="44"):
        """Ordinary new words of their author."""
        message = value["message"]
        self.assertEqual((value["_state"], message["action"], value["actor"]["platform_actor_id"]), ("ready", "observed", author))
        self.assertEqual(value["text"], message["raw"]["body"])
        self.assertIn("any_message", value["facts"])
        self.assertIsNone(message["previous_source_event_id"])
        for absent in ("forward_origin", "edited_at", "non_text"):
            self.assertNotIn(absent, message)
        self.assertNotIn("word_origin", message["fact_evidence"])
        self.assertNotIn("revision", [gap["scope"] for gap in message["coverage_gaps"]])

    def assert_not_own(self, value, code, *, capable=True, editors=None, error=None):
        """Still new words under their author (ordinary attention), never that account's own words."""
        message = value["message"]
        self.assertEqual((value["_state"], message["action"], value["actor"]["platform_actor_id"]), ("ready", "observed", "44"))
        self.assertEqual(value["text"], message["raw"]["body"])  # the exact current words
        self.assertTrue({"any_message", "my_thread"} <= set(value["facts"]))
        self.assertFalse(core_reads_as_change(message))  # no state_change, no predecessor: never a change
        self.assertNotIn("edited_at", message)
        self.assertEqual(message["original_author"]["platform_actor_id"], "44")
        self.assertEqual(message["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
        self.assertEqual(message["fact_evidence"]["word_origin"], {"code": code, **({"editors": editors} if editors else {})})
        gap = {"scope": "revision", "code": code, **({"error": error} if error else {})}
        self.assertEqual(gap in message["coverage_gaps"], code != "other_editor_recorded")  # a proven edit is no gap
        if capable:
            self.assertEqual([item["kind"] for item in message["non_text"]], ["change_attribution"])
        else:
            self.assertNotIn("non_text", message)  # a host without the capability reads forward_origin alone

    def test_words_nobody_else_can_have_changed_are_their_authors_without_a_read(self):
        review = {"id": 700, "node_id": "PRR_700", "body": "", "user": OWNER, "state": "COMMENTED", "submitted_at": self.EDITED}
        inline = {"id": 820, "node_id": "PRRC_820", "body": "Nit: rename this", "user": OWNER, "path": "a.py", "line": 3,
                  "created_at": self.WRITTEN, "updated_at": self.EDITED, "diff_hunk": "@@", "pull_request_review_id": 700,
                  "pull_request_url": "https://api.github.com/repos/example/project/pulls/2"}
        answer = {"id": "DC_640", "databaseId": 640, "body": "An answer", "createdAt": self.WRITTEN, "updatedAt": self.EDITED,
                  "lastEditedAt": None, "replyTo": None, "author": gql(OWNER)}
        for capable in (True, False):
            with self.subTest(capable=capable, row="comment never updated"):
                self.fresh(capable)
                self.assert_own(self.observe(self.note(updated=self.WRITTEN)))
                self.assertEqual(self.provider.record_calls, [])
            with self.subTest(capable=capable, row="inline comment published with its review"):
                self.fresh(capable)
                self.provider.rows = [pull(), inline, review]  # written in a pending review, visible once submitted
                self.assert_own(self.page("inline", [inline]))
                self.assertEqual(self.provider.record_calls, [])
            with self.subTest(capable=capable, row="discussion comment GitHub says was never edited"):
                self.fresh(capable)
                self.provider.rows = [{"id": "D_7", "databaseId": 507, "number": 7, "title": "Idea", "body": "Let us discuss",
                                       "createdAt": "2026-02-01T00:00:00Z", "updatedAt": self.EDITED, "lastEditedAt": None,
                                       "author": gql(HUMAN)}, answer]
                self.provider.graph_pages[("discussion_comments", "D_7", None)] = {"nodes": [answer], "pageInfo": {
                    "hasNextPage": False, "endCursor": None}}
                with self.store.transaction() as db:
                    self.store.job(db, "repo:321:fixture-discussion-comments", {
                        "kind": "discussion_comments", "repo": REPO, "number": 7, "discussion_id": "D_7", "node_id": "D_7"})
                self.assertTrue(self.tick_only("repo:321:fixture-discussion-comments"))
                self.assert_own(self.latest())
                self.assertEqual(self.provider.record_calls, [])

    def test_words_their_author_alone_edited_stay_their_new_words(self):
        own = {"edits": [revision(self.EDITED, OWNER), creation(self.WRITTEN, OWNER)]}
        for capable in (True, False):
            with self.subTest(capable=capable):
                self.fresh(capable)
                self.assert_own(self.observe(self.note(), own))
                self.assertEqual(self.provider.record_calls, [(("node", "IC_660"), None)])

    def test_another_accounts_edit_before_first_sight_is_never_the_authors_own_words(self):
        later = "2026-02-02T00:07:00Z"
        records = {"another account made the latest edit": {"edits": [revision(self.EDITED, OTHER), creation(self.WRITTEN, OWNER)]},
                   # The author editing last proves nothing about what the other account wrote before.
                   "its author made the latest edit": {"edits": [revision(later, OWNER), revision(self.EDITED, OTHER),
                                                               creation(self.WRITTEN, OWNER)]}}
        for name, record in records.items():
            for capable in (True, False):
                with self.subTest(record=name, capable=capable):
                    self.fresh(capable)
                    row = self.note(body="Please ship it today, and delete the old branch", updated=record["edits"][0]["edited_at"])
                    value = self.observe(row, record)
                    self.assert_not_own(value, "other_editor_recorded", capable=capable, editors=["33"])
                    if capable:
                        self.assertEqual(self.non_text(value, "change_attribution"), [
                            "First observed after it was edited: GitHub's records name @visitor (id 33) besides its author "
                            "@owner-account (id 44). Who wrote which part is not recorded separately."])
                    kept = json.loads(self.store.one("SELECT value FROM snapshots WHERE key LIKE '%:issue_comment:660'")["value"])
                    self.assertEqual(kept["foreign_words"], {"body": True})

    def test_the_authors_later_edit_of_words_first_seen_mixed_stays_mixed(self):
        edits = [revision(self.EDITED, OTHER), creation(self.WRITTEN, OWNER)]
        row = self.note(body="Please ship it today, and delete the old branch")
        self.assert_not_own(self.observe(row, {"edits": list(edits)}), "other_editor_recorded", editors=["33"])
        again = {**row, "body": row["body"] + ". Thanks", "updated_at": "2026-02-03T00:00:00Z"}
        # Even a record that lists only this latest edit cannot make the text its author's: its origin stays with it.
        value = self.observe(again, {"edits": [revision(again["updated_at"], OWNER)]})
        self.assertEqual((value["message"]["action"], value["actor"]["platform_actor_id"]), ("edited", "44"))
        self.assertEqual(value["message"]["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
        self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["code"], "earlier_revision_not_its_authors")

    def test_an_issue_first_seen_after_another_account_touched_its_text_or_title(self):
        def opened(**changes):
            return {"id": 105, "node_id": "I_105", "number": 5, "title": "Proposal", "body": "Opened by the owner", "user": OWNER,
                    "state": "open", "locked": False, "assignees": [], "created_at": self.WRITTEN, "updated_at": self.EDITED,
                    **changes}
        renamed = {"type": "RenamedTitleEvent", "created_at": self.EDITED, "actor": gql(OTHER), "current_title": "Proposal (v2)"}
        own_rename = {**renamed, "actor": gql(OWNER)}
        cases = (("its text", opened(body="Rewritten by a collaborator"),
                  {"edits": [revision(self.EDITED, OTHER), creation(self.WRITTEN, OWNER)], "events": []}, {"body": True}),
                 ("its title", opened(title="Proposal (v2)"), {"edits": [], "events": [renamed]}, {"title": True}))
        for name, raw, record, marks in cases:
            with self.subTest(touched=name):
                self.fresh()
                self.provider.records[("thread", 5)] = record
                self.provider.rows = [raw]
                value = self.page("issues", [raw])
                self.assertEqual(value["text"], raw["title"] + "\n\n" + raw["body"])
                self.assertTrue({"any_message", "new_issue"} <= set(value["facts"]))  # still a new issue, by its author
                self.assertEqual(value["message"]["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
                self.assertEqual(value["message"]["fact_evidence"]["word_origin"], {"code": "other_editor_recorded", "editors": ["33"]})
                self.assertFalse(core_reads_as_change(value["message"]))
                kept = json.loads(self.store.one("SELECT value FROM snapshots WHERE key LIKE '%:issue:105'")["value"])
                self.assertEqual(kept["foreign_words"], marks)
                self.assertEqual(self.provider.record_calls, [(("thread", 5), "2026-02-01T23:59:00+00:00")])  # since it was opened
        for name, record in (("a label, a comment: no edit at all", {"edits": [], "events": []}),
                             ("its author's own edit and rename", {"edits": [revision(self.EDITED, OWNER), creation(self.WRITTEN, OWNER)],
                                                                   "events": [own_rename]})):
            with self.subTest(touched=name):
                self.fresh()
                self.provider.records[("thread", 5)] = record
                raw = opened()
                self.provider.rows = [raw]
                value = self.page("issues", [raw])
                self.assertEqual((value["_state"], value["actor"]["platform_actor_id"]), ("ready", "44"))
                self.assertIn("new_issue", value["facts"])
                self.assertNotIn("forward_origin", value["message"])
                self.assertEqual(len(self.provider.record_calls), 1)

    def test_a_review_body_is_judged_by_its_own_record(self):
        review = {"id": 710, "node_id": "PRR_710", "body": "Looks right to me", "user": OWNER, "state": "COMMENTED",
                  "submitted_at": self.WRITTEN}
        for name, record, own in (("never edited", {"edits": []}, True),
                                  ("edited by another account", {"edits": [revision(self.EDITED, OTHER), creation(self.WRITTEN, OWNER)],
                                                                 "last_edited_at": self.EDITED}, False)):
            with self.subTest(record=name):
                self.fresh()
                self.provider.rows = [pull(), review]
                self.provider.records[("node", "PRR_710")] = record
                value = self.page("reviews", [review], number=2)
                self.assertEqual((value["_state"], value["actor"]["platform_actor_id"], value["text"]), ("ready", "44", review["body"]))
                self.assertEqual("forward_origin" not in value["message"], own)
                self.assertEqual(self.provider.record_calls, [(("node", "PRR_710"), None)])  # a review row has no update time

    def test_a_record_that_cannot_establish_the_words_leaves_them_unproven(self):
        gone = TransportError("no_longer_visible", Response(404, {}, {}))
        cases = (("more edits than the record's window", {"edits": [revision(self.EDITED, OWNER)], "edits_incomplete": True},
                  "edit_record_incomplete", None),
                 ("an editor GitHub does not number", {"edits": [{"edited_at": self.EDITED, "deleted": False, "editor": None},
                                                                 creation(self.WRITTEN, OWNER)]}, "edit_record_incomplete", None),
                 ("a record that cannot be read", gone, "edit_record_unavailable", "no_longer_visible"))
        for name, record, code, error in cases:
            for capable in (True, False):
                with self.subTest(record=name, capable=capable):
                    self.fresh(capable)
                    value = self.observe(self.note(), record)
                    self.assert_not_own(value, code, capable=capable, error=error)
                    if capable:
                        self.assertEqual(self.non_text(value, "change_attribution"), [
                            "First observed after it was edited; GitHub's records do not establish whose edits its words "
                            "carry (" + code + ")."])

    def test_a_record_that_fails_for_now_is_read_again_before_the_first_offer(self):
        foreign = {"edits": [revision(self.EDITED, OTHER), creation(self.WRITTEN, OWNER)]}
        own = {"edits": [revision(self.EDITED, OWNER), creation(self.WRITTEN, OWNER)]}
        for name, answers, check in (
                ("then names another account", [unavailable(), foreign],
                 lambda value: self.assert_not_own(value, "other_editor_recorded", editors=["33"])),
                ("then names its author alone", [unavailable(), own], self.assert_own),
                ("and never answers", [unavailable()] * (ENRICH_ATTEMPTS + 2),
                 lambda value: self.assert_not_own(value, "edit_record_unavailable", error="provider_http_502"))):
            with self.subTest(record=name):
                self.fresh()
                waiting = self.observe(self.note(), answers)
                self.assertEqual(waiting["_state"], "enriching")  # never offered while whose words they are is being read
                self.assertEqual([n["need"] for n in self.store.meta("enrich:" + waiting["source_event_id"])["needs"]], ["origin"])
                self.assertEqual(self.drain(), ENRICH_ATTEMPTS - 1 if name == "and never answers" else 1)
                done = self.latest()
                self.assertEqual(done["source_event_id"], waiting["source_event_id"])
                self.assertEqual(done["message"]["observed_at"], waiting["message"]["observed_at"])
                check(done)

    def test_an_update_time_that_moved_without_a_recorded_edit(self):
        # A REST comment updated after it was written whose record says it was never edited: read again (a fresh
        # edit may not show yet), then GitHub's record stands and the words are their author's.
        self.fresh()
        waiting = self.observe(self.note(), {"edits": [], "last_edited_at": None})
        self.assertEqual(waiting["_state"], "enriching")
        self.assertEqual(self.drain(), ENRICH_RECORD_ATTEMPTS - 1)
        self.assert_own(self.latest())
        # The edit shows on the second read: judged by it.
        self.fresh()
        self.observe(self.note(), [{"edits": []}, {"edits": [revision(self.EDITED, OTHER), creation(self.WRITTEN, OWNER)]}])
        self.assertEqual(self.drain(), 1)
        self.assert_not_own(self.latest(), "other_editor_recorded", editors=["33"])
        # An object that itself reports an edit (GraphQL lastEditedAt) whose record never shows one is never unedited.
        self.fresh()
        answer = {"id": "DC_641", "databaseId": 641, "body": "An answer, edited", "createdAt": self.WRITTEN,
                  "updatedAt": self.EDITED, "lastEditedAt": self.EDITED, "replyTo": None, "author": gql(OWNER)}
        self.provider.rows = [{"id": "D_7", "databaseId": 507, "number": 7, "title": "Idea", "body": "Let us discuss",
                               "createdAt": "2026-02-01T00:00:00Z", "updatedAt": self.EDITED, "lastEditedAt": None,
                               "author": gql(HUMAN)}, answer]
        self.provider.graph_pages[("discussion_comments", "D_7", None)] = {"nodes": [answer], "pageInfo": {
            "hasNextPage": False, "endCursor": None}}
        self.provider.records[("node", "DC_641")] = {"edits": [], "last_edited_at": None}
        with self.store.transaction() as db:
            self.store.job(db, "repo:321:fixture-discussion-comments", {
                "kind": "discussion_comments", "repo": REPO, "number": 7, "discussion_id": "D_7", "node_id": "D_7"})
        self.assertTrue(self.tick_only("repo:321:fixture-discussion-comments"))
        self.assertEqual(self.latest()["_state"], "enriching")
        self.assertEqual(self.drain(), ENRICH_RECORD_ATTEMPTS - 1)
        done = self.latest()
        self.assertEqual(done["message"]["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})
        self.assertIn({"scope": "revision", "code": "edit_record_missing"}, done["message"]["coverage_gaps"])
        self.assertIn("any_message", done["facts"])

    def test_new_speech_by_an_author_without_a_number_or_by_a_bot_keeps_ordinary_handling(self):
        self.provider.pages["/users/lookup-user"] = TransportError("no_longer_visible", Response(404, {}, {}))
        for name, user in (("no author at all", None), ("a login GitHub gives no number for", {"login": "lookup-user", "type": "User"}),
                           ("a Bot", {"id": 901, "login": "helper-app[bot]", "type": "Bot"})):
            for capable in (True, False):
                with self.subTest(author=name, capable=capable):
                    self.fresh(capable)
                    value = self.observe(self.note(user=user, body="@service-writer could you look?"))
                    self.assertEqual((value["_state"], value["message"]["action"]), ("ready", "observed"))
                    self.assertEqual(value["actor"].get("platform_actor_id"), "901" if name == "a Bot" else None)
                    self.assertEqual("from_bot" in value["facts"], name == "a Bot")
                    self.assertTrue({"any_message", "mention", "my_thread"} <= set(value["facts"]))
                    self.assertFalse(core_reads_as_change(value["message"]))
                    self.assertNotIn("forward_origin", value["message"])
                    self.assertEqual(self.provider.record_calls, [])  # no person's words are at stake: no read

    def test_words_released_before_their_record_was_read_are_never_their_authors_own(self):
        self.fresh()
        waiting = self.observe(self.note(), [unavailable()])
        self.assertEqual(waiting["_state"], "enriching")
        calls = len(self.provider.record_calls)
        self.assertEqual(self.collector.release_waiting(), [waiting["source_event_id"]])
        done = self.latest()
        self.assert_not_own(done, "edit_record_unavailable", error="provider_http_502")
        self.assertIn({"scope": "enrichment", "code": "released_before_retries_finished", "needs": ["origin"]},
                      done["message"]["coverage_gaps"])
        self.assertEqual(len(self.provider.record_calls), calls)  # released without a read

    def test_an_edit_observed_while_the_first_sight_is_still_being_read_proves_nothing(self):
        self.fresh()
        row = self.note()
        waiting = self.observe(row, [unavailable()])
        self.assertEqual(waiting["_state"], "enriching")
        again = {**row, "body": "Please ship it tomorrow", "updated_at": "2026-02-03T00:00:00Z"}
        value = self.observe(again, {"edits": [revision(again["updated_at"], OWNER)]})
        self.assertEqual((value["message"]["action"], value["actor"]["platform_actor_id"]), ("edited", "44"))
        self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["code"], "earlier_revision_unsettled")
        self.assertEqual(value["message"]["forward_origin"], {"sender_user": {"platform": "github", "attribution": "unknown"}})


class DiscussionTitleTests(AttentionCase):
    """GitHub records who edited a Discussion's text; nothing records who set its title, and nothing documents that
    ``lastEditedAt`` covers it (SOURCES.md). Only a Discussion nothing has happened to since it was opened is proven
    its author's words; any other is still ordinary new speech under its author, with an unknown origin."""
    UNKNOWN = {"sender_user": {"platform": "github", "attribution": "unknown"}}

    def discussion(self, **changes):
        return {"id": "D_7", "databaseId": 507, "number": 7, "title": "Idea", "body": "Let us discuss",
                "createdAt": "2026-02-01T00:00:00Z", "updatedAt": "2026-02-01T00:00:00Z", "lastEditedAt": None,
                "locked": False, "closed": False, "author": gql(OWNER), **changes}

    def fresh(self, capable=True):
        with self.store.transaction() as db:
            for table in ("snapshots", "jobs"):
                db.execute("DELETE FROM " + table)
            db.execute("DELETE FROM meta WHERE key LIKE 'enrich:%'")
            db.execute("DELETE FROM inbox WHERE id!='event-1'")
        self.capable(change=capable)
        self.provider.record_calls.clear()

    def observe(self, raw, record=None):
        if record is not None:
            self.provider.records[("node", raw["id"])] = record
        self.provider.rows = [raw]
        self.provider.graph_pages[("discussions", None, None)] = {"nodes": [raw], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        with self.store.transaction() as db:
            db.execute("DELETE FROM jobs")
            self.store.job(db, "repo:321:fixture-discussions", {"kind": "discussions", "repo": REPO})
        self.assertTrue(self.tick_only("repo:321:fixture-discussions"))
        return self.latest()

    def kept(self):
        value = json.loads(self.store.one("SELECT value FROM snapshots WHERE key LIKE '%:discussion:D_7'")["value"])
        return value.get("title_read"), value.get("foreign_words")

    def test_a_discussion_nothing_has_happened_to_is_its_authors_new_words(self):
        for capable in (True, False):
            with self.subTest(capable=capable):
                self.fresh(capable)
                value = self.observe(self.discussion())
                message = value["message"]
                self.assertEqual((value["_state"], message["action"], value["actor"]["platform_actor_id"]), ("ready", "observed", "44"))
                self.assertEqual(value["text"], "Idea\n\nLet us discuss")
                self.assertIn(SIGNAL_NEW_DISCUSSION, value["facts"])
                for absent in ("forward_origin", "non_text", "edited_at"):
                    self.assertNotIn(absent, message)
                self.assertNotIn("word_origin", message["fact_evidence"])
                self.assertEqual(message["coverage_gaps"], [])
                self.assertEqual((self.provider.record_calls, self.kept()), ([], (True, None)))

    def test_last_edited_at_never_proves_the_title_of_a_discussion_first_seen_after_something_happened_to_it(self):
        own = {"edits": [revision("2026-02-01T00:05:00Z", OWNER), creation("2026-02-01T00:00:00Z", OWNER)]}
        cases = (("its text never edited", self.discussion(updatedAt="2026-02-01T00:05:00Z")),
                 ("its text edited by its author alone", self.discussion(updatedAt="2026-02-01T00:05:00Z", body="Let us discuss it",
                                                                         lastEditedAt="2026-02-01T00:05:00Z")))
        for capable in (True, False):
            self.fresh(capable)
            plain = self.observe(self.discussion())["facts"]
            for name, raw in cases:
                with self.subTest(capable=capable, discussion=name):
                    self.fresh(capable)
                    value = self.observe(raw, own)
                    message = value["message"]
                    # Ordinary new speech under its author: the same attention as the untouched one, never a change.
                    self.assertEqual((value["_state"], message["action"], value["actor"]["platform_actor_id"]),
                                     ("ready", "observed", "44"))
                    self.assertEqual((value["text"], value["facts"]), (raw["title"] + "\n\n" + raw["body"], plain))
                    self.assertIn(SIGNAL_NEW_DISCUSSION, value["facts"])
                    self.assertFalse(core_reads_as_change(message))
                    self.assertNotIn("edited_at", message)
                    self.assertEqual(message["original_author"]["platform_actor_id"], "44")
                    # Whose words they are is not established: never that account's own words.
                    self.assertEqual(message["forward_origin"], self.UNKNOWN)
                    self.assertEqual(message["fact_evidence"]["word_origin"], {"code": "title_record_unavailable"})
                    self.assertEqual(message["coverage_gaps"], [{"scope": "revision", "code": "title_record_unavailable"}])
                    if capable:
                        self.assertEqual(self.non_text(value, "change_attribution"), [
                            "First observed after something had happened to it; GitHub keeps no record of who set a "
                            "Discussion's title, so its words are not established as its author's alone (title_record_unavailable)."])
                        self.assertEqual([item["kind"] for item in message["non_text"]], ["change_attribution"])
                    else:
                        self.assertNotIn("non_text", message)
                    # No record can answer for a title: none is read, and the mark stays with the object.
                    self.assertEqual((self.provider.record_calls, self.kept()), ([], (True, {"title": True})))

    def test_the_authors_later_edit_follows_what_was_established_at_first_sight(self):
        for name, first, own in (("first seen untouched", self.discussion(), True),
                                 ("first seen after something happened to it", self.discussion(updatedAt="2026-02-01T00:05:00Z"), False)):
            with self.subTest(discussion=name):
                self.fresh()
                self.observe(first)
                edited = {**first, "body": "Clarified by its author", "updatedAt": "2026-02-02T00:00:00Z",
                          "lastEditedAt": "2026-02-02T00:00:00Z"}
                value = self.observe(edited, {"edits": [revision(edited["updatedAt"], OWNER), creation(first["createdAt"], OWNER)]})
                self.assertEqual((value["message"]["action"], value["actor"]["platform_actor_id"]), ("edited", "44"))
                self.assertEqual(value["text"], "Idea\n\nClarified by its author")
                if own:  # the genuine owner-only path: opened and edited by that account alone
                    self.assertNotIn("forward_origin", value["message"])
                    self.assertNotIn("word_origin", value["message"]["fact_evidence"])
                else:
                    self.assertEqual(value["message"]["forward_origin"], self.UNKNOWN)
                    self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["code"], "earlier_revision_not_its_authors")

    def test_a_title_changed_between_two_observations_is_never_taken_for_the_authors(self):
        self.fresh()
        opened = self.discussion()
        self.observe(opened)
        renamed = {**opened, "title": "Idea, retitled", "updatedAt": "2026-02-02T00:00:00Z"}  # lastEditedAt stays null
        self.observe(renamed, {"edits": []})
        self.assertEqual(self.kept(), (True, {"title": True}))
        edited = {**renamed, "body": "Clarified by its author", "updatedAt": "2026-02-03T00:00:00Z", "lastEditedAt": "2026-02-03T00:00:00Z"}
        value = self.observe(edited, {"edits": [revision(edited["updatedAt"], OWNER), creation(opened["createdAt"], OWNER)]})
        self.assertEqual(value["actor"]["platform_actor_id"], "44")
        self.assertEqual(value["message"]["forward_origin"], self.UNKNOWN)
        self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["code"], "earlier_revision_not_its_authors")

    def test_a_discussion_retained_before_its_title_was_ever_judged_never_proves_it(self):
        """A baseline, or a snapshot an earlier release kept: nothing can be read for its title afterwards."""
        self.fresh()
        old = self.discussion(createdAt="2025-12-01T00:00:00Z", updatedAt="2025-12-05T00:00:00Z")
        self.observe(old)  # older than the connector's first scan: retained, never offered
        self.assertEqual(([e["_state"] for e in self.events()], self.kept()), (["baseline"], (None, None)))
        edited = {**old, "body": "Clarified by its author", "updatedAt": "2026-02-02T00:00:00Z", "lastEditedAt": "2026-02-02T00:00:00Z"}
        edits = [revision(edited["updatedAt"], OWNER), creation(old["createdAt"], OWNER)]
        value = self.observe(edited, {"edits": list(edits)})
        self.assertEqual((value["_state"], value["message"]["action"], value["actor"]["platform_actor_id"]), ("ready", "edited", "44"))
        self.assertEqual(value["message"]["forward_origin"], self.UNKNOWN)
        self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["code"], "title_not_its_authors")
        self.assertEqual(self.kept(), (True, {"title": True}))
        again = {**edited, "body": "Clarified once more", "updatedAt": "2026-02-03T00:00:00Z", "lastEditedAt": "2026-02-03T00:00:00Z"}
        value = self.observe(again, {"edits": [revision(again["updatedAt"], OWNER)] + edits})
        self.assertEqual(value["message"]["fact_evidence"]["word_origin"]["code"], "earlier_revision_not_its_authors")


class OutsideEditedMentionTests(OutsideCase):
    def test_a_comment_written_by_one_account_and_edited_by_another_is_unattributed(self):
        self.capable()
        for author, editor in ((WRITER, OTHER), (OTHER, WRITER)):
            with self.subTest(author=author["login"], editor=editor["login"]):
                with self.store.transaction() as db:
                    db.execute("DELETE FROM snapshots")
                    db.execute("DELETE FROM jobs")
                    db.execute("DELETE FROM inbox WHERE id!='event-1'")
                row = note(910, author, "@contributor please review", "2026-02-02T00:00:00Z", "2026-02-02T00:00:30Z")
                self.provider.rows = [pull(), row]
                self.provider.records[("node", "IC_910")] = {"edits": [revision("2026-02-02T00:00:30Z", editor),
                                                                       creation("2026-02-02T00:00:00Z", author)]}
                self.discover(updated="2026-02-02T00:00:31Z")
                item, = self.mention_events()
                # Neither the comment's author nor its editor is taken for whoever added the mention.
                self.assertEqual(item["actor"], {"platform": "github", "attribution": "unknown"})
                self.assertTrue(core_reads_as_change(item["message"]))
                self.assertIn({"scope": "actor", "code": "multiple_change_actors"}, item["message"]["coverage_gaps"])
                self.assertEqual(item["message"]["original_author"]["platform_actor_id"], str(author["id"]))
                self.assertEqual(item["message"]["forward_origin"]["sender_user"]["id"], str(author["id"]))

    def test_new_words_edited_only_by_their_author_stay_their_new_words(self):
        self.capable()
        row = note(911, OTHER, "@contributor please review", "2026-02-02T00:00:00Z", "2026-02-02T00:00:30Z")
        self.provider.rows = [pull(), row]
        self.provider.records[("node", "IC_911")] = {"edits": [revision("2026-02-02T00:00:30Z", OTHER),
                                                               creation("2026-02-02T00:00:00Z", OTHER)]}
        self.discover(updated="2026-02-02T00:00:31Z")
        item, = self.mention_events()
        self.assertEqual(item["actor"]["platform_actor_id"], "33")
        self.assertFalse(core_reads_as_change(item["message"]))

    def test_a_mention_an_edit_added_to_an_older_comment_is_the_editors(self):
        self.capable()
        base = datetime.now(timezone.utc) - timedelta(days=1)
        self.store.set_meta("bootstrap:notifications:22", stamp(base))
        written = stamp(base + timedelta(hours=1))
        row = note(912, OTHER, "Looks good", written)
        self.provider.rows = [pull(), row]
        first = self.discover(previous=None, updated=stamp(base + timedelta(hours=2)))
        self.assertEqual(self.items(), [])
        read_at = datetime.fromisoformat(first["conversation"]["history"]["observed_at"])
        edit_at = stamp(read_at + timedelta(minutes=1))
        changed = {**row, "body": "Looks good, @contributor can you merge?", "updated_at": edit_at}
        self.provider.rows = [pull(), changed]
        self.provider.records[("node", "IC_912")] = {"edits": [revision(edit_at, MAINTAINER), creation(written, OTHER)]}
        second = self.discover(previous=stamp(base + timedelta(hours=2)), updated=stamp(read_at + timedelta(minutes=2)))
        item, = self.mention_events()
        message = item["message"]
        self.assertEqual(item["actor"]["platform_actor_id"], "55")  # the edit that added the words, not their author
        self.assertEqual(message["action"], "edited")
        self.assertEqual(message["original_author"]["platform_actor_id"], "33")
        self.assertEqual(message["forward_origin"]["sender_user"]["id"], "33")
        self.assertEqual(message["edited_at"], edit_at.replace("Z", "+00:00"))
        self.assertEqual(self.non_text(item), ["Edited this comment to mention @contributor (id 22)."])
        self.assertTrue(core_reads_as_change(message))
        self.assertEqual(second["message"]["fact_evidence"]["items"], [item["source_event_id"]])

    def test_an_older_comment_edited_without_an_earlier_copy_is_an_explicit_gap(self):
        self.capable()
        self.store.set_meta("bootstrap:notifications:22", "2026-02-01T12:00:00+00:00")
        row = note(913, OTHER, "Now asking @contributor", "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z")
        self.provider.rows = [pull(), row]
        self.provider.records[("node", "IC_913")] = {"edits": [revision("2026-02-02T00:00:00Z", OTHER),
                                                               creation("2026-02-01T00:00:00Z", OTHER)]}
        envelope = self.discover(previous=None)
        self.assertEqual(self.items(), [])
        self.assertIn({"scope": "notification", "code": "mention_revision_unproven", "message_id": "IC_913"},
                      envelope["message"]["coverage_gaps"])


class MentionAddedBackTests(OutsideCase):
    """A reported mention that an observed edit removed and a later edit added back is addressed again: the words
    last observed of the message are kept apart from the event last sent for it."""

    def setUp(self):
        super().setUp()
        self.capable()
        self.base = datetime.now(timezone.utc) - timedelta(days=1)
        for reader in (11, 22):
            self.store.set_meta("bootstrap:notifications:" + str(reader), stamp(self.base))
        self.written = stamp(self.base + timedelta(hours=1))
        self.updates = {11: None, 22: None}

    def update(self, reader=22):
        """This reader's next notification update of the thread, read now."""
        previous, self.updates[reader] = self.updates[reader], between_reads()
        return self.discover(reader, previous=previous, updated=self.updates[reader])

    def edit(self, row, body, editor, edits):
        at = between_reads()
        edits.insert(0, revision(at, editor))
        changed = {**row, "body": body, "updated_at": at}
        self.provider.rows = [pull(), changed]
        self.provider.records[("node", row["node_id"])] = {"edits": list(edits)}
        return changed, at

    def test_a_mention_removed_and_added_back_is_reported_again(self):
        for first, without in (("@contributor please look", "please look"),
                               ("@service-writer and @contributor, thoughts?", "@service-writer, thoughts?")):
            with self.subTest(first=first):
                with self.store.transaction() as db:
                    db.execute("DELETE FROM snapshots")
                    db.execute("DELETE FROM jobs")
                    db.execute("DELETE FROM inbox WHERE id!='event-1'")
                self.updates = {11: None, 22: None}
                row = note(914, OTHER, first, self.written)
                edits = [creation(self.written, OTHER)]
                self.provider.rows = [pull(), row]
                self.update()
                reported, = self.mention_events()
                self.assertEqual(reported["message"]["revision"], 1)
                # The mention of @contributor is edited away, and both readers observe the thread without it.
                row, _ = self.edit(row, without, OTHER, edits)
                self.update(22)
                self.update(11)
                self.assertEqual(len(self.mention_events()), 1)  # a removal addresses nobody: no event
                # A maintainer's edit adds it back, in exactly the words first reported.
                row, back_at = self.edit(row, first, MAINTAINER, edits)
                envelope = self.update(22)
                again = self.mention_events()[-1]
                self.assertEqual(len(self.mention_events()), 2)
                message = again["message"]
                self.assertEqual((again["facts"], again["actor"]["platform_actor_id"]), (["mention"], "55"))
                self.assertEqual((message["action"], message["revision"]), ("edited", 2))
                self.assertEqual(message["previous_source_event_id"], reported["source_event_id"])  # the event last sent
                self.assertEqual(message["message_id"], "IC_914")
                self.assertEqual(message["edited_at"], back_at)
                self.assertEqual(message["forward_origin"]["sender_user"]["id"], "33")
                self.assertEqual([m["id"] for m in message["mentions"] if m["id"] == "22"], ["22"])
                self.assertEqual(self.non_text(again), ["Edited this comment to mention @contributor (id 22)."])
                self.assertEqual(message["coverage_gaps"], [{"scope": "permission", "code": "repository_not_selected_for_writes"}])
                self.assertEqual(envelope["message"]["fact_evidence"]["items"], [again["source_event_id"]])
                # The other reader's notification of the same edit, and a repeat for this one, add no second event.
                self.update(11)
                self.update(22)
                self.assertEqual(len(self.mention_events()), 2)
                self.assertEqual(self.mention_events()[-1]["source_event_id"], again["source_event_id"])

    def test_words_changed_without_a_new_mention_follow_the_message_without_an_event(self):
        row = note(916, OTHER, "@contributor please look", self.written)
        edits = [creation(self.written, OTHER)]
        self.provider.rows = [pull(), row]
        self.update()
        reported, = self.mention_events()
        key = self.store.one("SELECT key FROM snapshots WHERE key LIKE '%:outside:%IC_916'")["key"]
        before = self.store.one("SELECT revision,observed FROM snapshots WHERE key=?", (key,))
        row, _ = self.edit(row, "@contributor please look at the second commit", OTHER, edits)
        self.update()
        after = self.store.one("SELECT revision,value,observed FROM snapshots WHERE key=?", (key,))
        self.assertEqual(len(self.mention_events()), 1)  # still mentioned, nobody newly named
        self.assertEqual(after["revision"], before["revision"])  # its revision still names the event last sent
        self.assertEqual(json.loads(after["value"])["text"], row["body"])
        self.assertGreater(after["observed"], before["observed"])
        self.assertEqual(dump(self.mention_events()[0]), dump(reported))  # the sent event is never rewritten


class ReadTimeTests(OutsideCase):
    """An event completed after a retried read says when its thread was really read, never when it was rebuilt."""

    def test_an_edit_made_between_the_read_and_the_rebuild_is_still_after_that_read(self):
        self.capable()
        base = datetime.now(timezone.utc) - timedelta(days=1)
        self.store.set_meta("bootstrap:notifications:22", stamp(base))
        written = stamp(base + timedelta(hours=1))
        row = note(915, OTHER, "Looks good", written)
        self.provider.rows = [pull(), row]
        self.provider.records[("thread", 2)] = [unavailable(), {"events": []}]
        waiting = self.discover(previous=None, updated=stamp(base + timedelta(hours=2)))
        self.assertEqual(waiting["_state"], "enriching")
        read_at = waiting["conversation"]["history"]["observed_at"]
        # The comment is edited to mention a watched account while the notification still waits for its record.
        edit_at = between_reads()
        self.assertTrue(self.enrich())
        done = self.envelope()
        self.assertEqual((done["_state"], done["source_event_id"]), ("ready", waiting["source_event_id"]))
        self.assertGreater(edit_at, read_at)
        self.assertEqual(done["conversation"]["history"]["observed_at"], read_at)  # the thread was not read again
        self.assertEqual(done["conversation"]["thread_history"]["observed_at"], read_at)
        self.assertEqual(done["message"]["observed_at"], waiting["message"]["observed_at"])
        changed = {**row, "body": "Looks good, @contributor can you merge?", "updated_at": edit_at}
        self.provider.rows = [pull(), changed]
        self.provider.records[("node", "IC_915")] = {"edits": [revision(edit_at, MAINTAINER), creation(written, OTHER)]}
        self.provider.records[("thread", 2)] = {"events": []}
        second = self.discover(previous=stamp(base + timedelta(hours=2)),
                               updated=stamp(datetime.now(timezone.utc) + timedelta(seconds=1)))
        item, = self.mention_events()
        self.assertEqual((item["actor"]["platform_actor_id"], item["message"]["action"]), ("55", "edited"))
        self.assertEqual(second["_state"], "ready")
        self.assertNotIn("mention_revision_unproven", [gap["code"] for gap in second["message"]["coverage_gaps"]])


class OneMessageTwoReadersTests(OutsideCase):
    def test_one_comment_naming_both_readers_is_one_event_with_both_readers_evidence(self):
        self.capable()
        row = note(920, OTHER, "@service-writer and @contributor, thoughts?", "2026-02-02T00:00:00Z")
        self.provider.rows = [pull(), row]
        first = self.discover(22)
        second = self.discover(11)
        item, = self.mention_events()
        self.assertEqual(item["message"]["mentions"], [{"id": "11", "name": "service-writer", "is_self": True},
                                                       {"id": "22", "name": "contributor", "is_self": False}])
        self.assertEqual(item["message"]["message_id"], "IC_920")
        for envelope in (first, second):  # each reader's notification stays its own evidence of that one event
            self.assertEqual(envelope["facts"], [])
            self.assertEqual(envelope["message"]["fact_evidence"]["items"], [item["source_event_id"]])
        self.assertEqual({first["message"]["recipient_id"], second["message"]["recipient_id"]}, {11, 22})
        offered = item["source_event_id"], dump(item)
        # A restart and a repeated discovery add nothing and rewrite nothing.
        self.store.close()
        self.store = Store(self.scratch.name)
        self.outbox.store = self.store
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, {11: self.reader, 22: self.reader})
        self.discover(11, updated="2026-02-02T00:05:00Z")
        again, = self.mention_events()
        self.assertEqual((again["source_event_id"], dump(again)), offered)


class NotificationUpdateIsNoRevisionTests(OutsideCase):
    def test_a_later_comment_by_an_author_without_an_id_stays_new_words(self):
        self.capable()
        known = note(930, OTHER, "@contributor first question", "2026-02-02T00:00:00Z")
        self.provider.rows = [pull(), known]
        first = self.discover(updated="2026-02-02T00:00:01Z")
        unnamed = note(931, None, "@contributor a second question", "2026-02-03T00:00:00Z")
        self.provider.rows = [pull(), known, unnamed]
        second = self.discover(previous="2026-02-02T00:00:01Z", updated="2026-02-03T00:00:01Z")
        later = next(item for item in self.mention_events() if item["message"]["message_id"] == "IC_931")
        self.assertEqual(later["actor"], {"platform": "github", "attribution": "unknown"})
        self.assertIsNone(later["message"]["previous_source_event_id"])
        self.assertFalse(core_reads_as_change(later["message"]))  # ordinary attention to new words
        self.assertIsNone(second["message"]["previous_source_event_id"])
        self.assertEqual(second["message"]["fact_evidence"]["previous_notification_event_id"], first["source_event_id"])
        self.assertEqual(second["message"]["fact_evidence"]["items"], [later["source_event_id"]])

    def test_an_update_has_one_form_whether_built_at_once_or_after_a_retry(self):
        self.capable()
        first = self.discover(updated="2026-02-02T00:00:01Z")
        at_once = self.discover(previous="2026-02-02T00:00:01Z", updated="2026-02-03T00:00:01Z")
        self.provider.records[("thread", 2)] = [unavailable(), {"events": []}]
        waiting = self.discover(previous="2026-02-03T00:00:01Z", updated="2026-02-04T00:00:01Z")
        self.assertEqual(waiting["_state"], "enriching")
        self.assertTrue(self.enrich())
        retried = self.envelope()
        self.assertEqual(retried["source_event_id"], waiting["source_event_id"])
        for value, previous in ((first, None), (at_once, first), (retried, at_once)):
            message = value["message"]
            self.assertEqual((value["_state"], message["action"], message["previous_source_event_id"]), ("ready", "observed", None))
            self.assertEqual(message["fact_evidence"].get("previous_notification_event_id"),
                             previous and previous["source_event_id"])

    def test_a_host_without_the_capability_keeps_the_0_1_6_update_form(self):
        first = self.discover(updated="2026-02-02T00:00:01Z")
        second = self.discover(previous="2026-02-02T00:00:01Z", updated="2026-02-03T00:00:01Z")
        self.assertEqual((first["message"]["action"], second["message"]["action"]), ("observed", "edited"))
        self.assertEqual(second["message"]["previous_source_event_id"], first["source_event_id"])
        self.assertEqual(second["facts"], ["any_message"])


class RemovedReaderTests(OutsideCase):
    def test_a_notification_waiting_when_its_reader_is_removed_goes_out_without_any_read(self):
        """Settings remove the Notifications reader while one of its notifications waits for a read: it goes out now
        with what was read and each open read as a gap, never with a read on that reader's behalf."""
        self.capable()
        self.provider.records[("thread", 2)] = [unavailable()]
        waiting = self.discover()
        self.assertEqual(waiting["_state"], "enriching")
        job = self.store.one("SELECT id FROM jobs WHERE stream='notifications:22:enrich'")["id"]
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": [], "notifications_user_id": 0},
                                   {11: self.reader})
        calls = list(self.provider.record_calls)
        self.provider.snapshot_errors = [AssertionError("the thread is never read again")]
        self.assertTrue(self.enrich())
        done = self.envelope()
        self.assertEqual((done["source_event_id"], done["_state"], done["facts"]), (waiting["source_event_id"], "ready", []))
        gaps = done["message"]["coverage_gaps"]
        self.assertIn({"scope": "notification", "code": "addressed_action_unverified", "need": "thread_record",
                       "error": "provider_http_502"}, gaps)
        self.assertIn({"scope": "enrichment", "code": "notification_recipient_removed", "needs": ["thread_record"]}, gaps)
        self.assertEqual((self.provider.record_calls, len(self.provider.snapshot_errors)), (calls, 1))  # no read at all
        self.assertEqual(self.items(), [])
        self.assertIsNone(self.store.meta("enrich:" + waiting["source_event_id"]))
        self.assertEqual(self.store.one("SELECT state FROM jobs WHERE id=?", (job,))["state"], "done")
        self.assertIsNone(self.store.one("SELECT scope FROM gaps WHERE scope=? AND resolved IS NULL", ("page:" + str(job),)))
        sent = []
        host = Host(SimpleNamespace(request=lambda method, path, payload: sent.append(payload) or Response(
            200, {"status": "completed", "outcome": "silent"}, {})), self.store, self.outbox)
        host.dispatch()
        host.close()
        self.assertEqual([p["event"]["source_event_id"] for p in sent], [waiting["source_event_id"]])
        # The reader configured again later finds nothing of it left to resume.
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, {11: self.reader, 22: self.reader})
        self.collector.seed()
        self.assertEqual(self.store.one("SELECT state FROM jobs WHERE id=?", (job,))["state"], "done")

    def test_a_removed_readers_own_reads_still_wait_for_it(self):
        """Its Notifications list and a discovery not yet observed hold no event: they stay for the reader's return."""
        self.capable()
        stream = "notifications:22:children"
        with self.store.transaction() as db:
            self.store.job(db, stream, {"kind": "notification_discovery", "recipient": 22, "notification": self.notification()})
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": [], "notifications_user_id": 0},
                                   {11: self.reader})
        self.assertFalse(self.tick_only(stream))
        self.assertEqual(self.store.one("SELECT state FROM jobs WHERE stream=?", (stream,))["state"], "removed")
        self.assertEqual(self.store.one("SELECT code FROM gaps WHERE scope LIKE 'page:%' AND resolved IS NULL")["code"],
                         "notification_recipient_removed")
        self.assertIsNone(self.envelope())

    def clear_reader(self):
        """Settings remove reader 22, and no watched account is that account: no account Settings name is it now."""
        config = {**CONFIG, "repositories": [], "notifications_user_id": 0,
                  "watched_identities": [user for user in CONFIG["watched_identities"] if user["id"] != HUMAN["id"]]}
        self.collector = Collector(self.store, self.provider, config, {11: self.reader})

    def released_without_a_read(self):
        calls = list(self.provider.record_calls)
        self.provider.snapshot_errors = [AssertionError("the thread is never read again")]
        self.provider.get = lambda path: self.fail("read for a removed reader: " + path)
        self.assertTrue(self.enrich())
        self.assertEqual((self.provider.record_calls, len(self.provider.snapshot_errors)), (calls, 1))  # no read at all
        return self.envelope()

    def test_a_cleared_readers_open_read_of_words_that_named_only_it_stays_an_explicit_gap(self):
        """Its waiting notification still read the edit record of words naming only that reader. Proven again under
        Settings that name it no more, those words name no account: the read it left open stays an explicit gap."""
        self.capable()
        first = self.discover(updated="2026-02-02T00:00:01Z")
        offered = dump(first)
        self.provider.rows = [pull(), note(990, OTHER, "@contributor please look", "2026-02-03T00:00:00Z",
                                           updated="2026-02-03T00:05:00Z")]
        self.provider.records[("node", "IC_990")] = [unavailable()]
        waiting = self.discover(previous="2026-02-02T00:00:01Z", updated="2026-02-03T00:10:00Z")
        self.assertEqual(waiting["_state"], "enriching")
        self.assertEqual([need["need"] for need in self.store.meta("enrich:" + waiting["source_event_id"])["needs"]],
                         ["edits:IC_990"])
        self.clear_reader()
        done = self.released_without_a_read()
        self.assertEqual((done["source_event_id"], done["_state"], done["facts"]), (waiting["source_event_id"], "ready", []))
        gaps = done["message"]["coverage_gaps"]
        self.assertIn({"scope": "notification", "code": "addressed_action_unverified", "need": "edits:IC_990",
                       "error": "provider_http_502"}, gaps)
        self.assertIn({"scope": "enrichment", "code": "notification_recipient_removed", "needs": ["edits:IC_990"]}, gaps)
        self.assertEqual(self.items(), [])  # nothing is proven for an account no Settings name
        kept = self.store.one("SELECT state,event FROM inbox WHERE id=?", (first["source_event_id"],))
        self.assertEqual(dump(dict(json.loads(kept["event"]), _state=kept["state"])), offered)  # never rewritten

    def test_a_cleared_readers_open_read_of_an_action_addressed_only_to_it_stays_an_explicit_gap(self):
        self.capable()
        self.provider.pages["/users/lookup-user"] = unavailable()
        self.provider.records[("thread", 2)] = {"events": [
            {"type": "AssignedEvent", "id": "AE_9", "created_at": "2026-02-02T00:00:00Z",
             "actor": {"login": "lookup-user", "__typename": "User"}, "assignee": target(HUMAN)}]}
        waiting = self.discover()
        self.assertEqual([need["need"] for need in self.store.meta("enrich:" + waiting["source_event_id"])["needs"]],
                         ["actor:AE_9"])
        self.clear_reader()
        done = self.released_without_a_read()
        self.assertEqual((done["source_event_id"], done["_state"]), (waiting["source_event_id"], "ready"))
        gaps = done["message"]["coverage_gaps"]
        self.assertIn({"scope": "notification", "code": "addressed_action_unverified", "need": "actor:AE_9",
                       "error": "provider_http_502"}, gaps)
        self.assertIn({"scope": "enrichment", "code": "notification_recipient_removed", "needs": ["actor:AE_9"]}, gaps)
        self.assertEqual(self.items(), [])

    def test_a_reader_still_named_without_its_credential_keeps_its_work(self):
        """The companion could not build reader 22 (its credential missing) while Settings still name it. What it
        already took is proven exactly as with its reader present, reading through the writer, never released; only
        its own discovery waits, under its own code, for the restart that restores the credential."""
        outcomes = []
        for present in (True, False):
            with self.subTest(reader="present" if present else "credential missing"):
                if not present:
                    self.tearDown()
                    self.setUp()
                self.capable()
                self.provider.records[("thread", 2)] = [unavailable(), {"events": [
                    {"type": "ReviewRequestedEvent", "id": "RRE_7", "created_at": "2026-02-02T00:00:00Z",
                     "actor": gql(OTHER), "reviewer": target(HUMAN)}]}]
                waiting = self.discover()
                self.assertEqual(waiting["_state"], "enriching")
                readers = {11: self.reader, 22: self.reader} if present else {11: self.reader}
                self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, readers)
                calls = len(self.provider.record_calls)
                self.assertTrue(self.enrich())
                self.assertEqual(len(self.provider.record_calls), calls + 1)  # read as ever, through the writer
                done, (item,) = self.envelope(), self.items()
                self.assertEqual((done["source_event_id"], done["_state"]), (waiting["source_event_id"], "ready"))
                self.assertEqual((item["facts"], item["actor"]["platform_actor_id"]), (["review_requested"], "33"))
                self.assertEqual(done["message"]["fact_evidence"]["items"], [item["source_event_id"]])
                self.assertNotIn("enrichment", [gap["scope"] for gap in done["message"]["coverage_gaps"]])
                outcomes.append([dump(value["message"]["coverage_gaps"]) for value in (done, item)])
        self.assertEqual(outcomes, outcomes[:1] * 2)
        stream = "notifications:22:children"
        with self.store.transaction() as db:
            self.store.job(db, stream, {"kind": "notification_discovery", "recipient": 22,
                                        "notification": self.notification(updated="2026-02-03T00:00:01Z")})
        self.assertFalse(self.tick_only(stream))
        job = self.store.one("SELECT id,state FROM jobs WHERE stream=? AND state!='done'", (stream,))
        self.assertEqual(job["state"], "removed")
        scope = "page:" + str(job["id"])
        self.assertEqual(self.store.one("SELECT code FROM gaps WHERE scope=? AND resolved IS NULL", (scope,))["code"],
                         "notification_reader_unavailable")
        # The restart that restores its credential builds the reader again: its discovery resumes and completes.
        self.provider.records[("thread", 2)] = {"events": []}
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, {11: self.reader, 22: self.reader})
        self.collector.seed()
        self.assertTrue(self.tick_only(stream))
        self.assertEqual(self.store.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"], "done")
        self.assertIsNone(self.store.one("SELECT scope FROM gaps WHERE scope=? AND resolved IS NULL", (scope,)))
        resumed = self.envelope()
        self.assertEqual(resumed["_state"], "ready")
        self.assertNotEqual(resumed["source_event_id"], done["source_event_id"])


class ProvenMentionSurvivesTests(OutsideCase):
    def test_a_failed_timeline_read_delays_only_what_depends_on_it(self):
        self.capable()
        row = note(940, OTHER, "@contributor look", "2026-02-02T00:00:00Z")
        self.provider.rows = [pull(), row]
        self.provider.records[("thread", 2)] = [unavailable()] * (ENRICH_ATTEMPTS + 2)
        waiting = self.discover()
        item, = self.mention_events()
        offered = dump(item)
        self.assertEqual((item["_state"], item["facts"], item["actor"]["platform_actor_id"]), ("ready", ["mention"], "33"))
        self.assertEqual(waiting["_state"], "enriching")
        runs = 0
        while self.envelope()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.enrich()
        done = self.envelope()
        self.assertEqual(done["source_event_id"], waiting["source_event_id"])
        self.assertIn({"scope": "notification", "code": "addressed_action_unverified", "need": "thread_record",
                       "error": "provider_http_502"}, done["message"]["coverage_gaps"])
        self.assertEqual(done["message"]["fact_evidence"]["items"], [item["source_event_id"]])
        kept, = self.mention_events()
        self.assertEqual(dump(kept), offered)  # the proven mention went out once and was never rewritten

    def test_a_proven_mention_and_a_later_proven_request_are_both_kept(self):
        self.capable()
        self.provider.rows = [pull(), note(941, OTHER, "@contributor look", "2026-02-02T00:00:00Z")]
        self.provider.records[("thread", 2)] = [unavailable(), {"events": [
            {"type": "ReviewRequestedEvent", "id": "RRE_3", "created_at": "2026-02-02T00:00:00Z", "actor": gql(OTHER),
             "reviewer": target(HUMAN)}]}]
        self.discover()
        self.assertTrue(self.enrich())
        self.assertEqual(sorted(item["facts"][0] for item in self.items()), ["mention", "review_requested"])
        self.assertEqual(len(self.envelope()["message"]["fact_evidence"]["items"]), 2)


class IncompleteTimelineTests(OutsideCase):
    def page_of(self, events, earlier):
        return {"events": events, "edits": [], "events_incomplete": earlier, "events_cursor": "cursor-" + str(len(events)) if earlier else None}

    def assigned(self, at="2026-02-02T00:00:00Z", ident="AE_1"):
        return {"type": "AssignedEvent", "id": ident, "created_at": at, "actor": gql(OTHER), "assignee": target(HUMAN)}

    def unrelated(self, n):
        return [{"type": "LockedEvent", "created_at": "2026-02-02T00:00:%02dZ" % (i % 60), "actor": gql(OTHER)} for i in range(n)]

    def test_an_earlier_timeline_page_is_read_back_for_the_action(self):
        self.capable()
        self.provider.records[("thread", 2)] = [self.page_of(self.unrelated(100), True), self.page_of([self.assigned()], False)]
        envelope = self.discover()
        item, = self.items()
        self.assertEqual((item["facts"], item["actor"]["platform_actor_id"]), (["assigned"], "33"))
        self.assertEqual(self.provider.record_calls[-1][2], "cursor-100")
        self.assertNotIn("addressed_action_record_incomplete", [g["code"] for g in envelope["message"]["coverage_gaps"]])

    def test_a_record_still_incomplete_past_its_pages_is_a_gap_not_an_empty_proof(self):
        self.capable()
        self.provider.records[("thread", 2)] = [self.page_of(self.unrelated(100), True)] * OUTSIDE_TIMELINE_PAGES
        envelope = self.discover()
        self.assertEqual(self.items(), [])
        self.assertEqual(len(self.provider.record_calls), OUTSIDE_TIMELINE_PAGES)
        self.assertIn({"scope": "notification", "code": "addressed_action_record_incomplete", "pages": OUTSIDE_TIMELINE_PAGES},
                      envelope["message"]["coverage_gaps"])

    def test_an_action_the_reason_names_is_reread_until_visible(self):
        self.capable()
        self.provider.records[("thread", 2)] = [self.page_of([], False), self.page_of([self.assigned()], False)]
        waiting = self.discover(reason="assign")
        self.assertEqual((waiting["_state"], self.items()), ("enriching", []))
        self.assertTrue(self.enrich())
        item, = self.items()
        self.assertEqual(item["facts"], ["assigned"])
        self.assertEqual(self.envelope()["_state"], "ready")

    def test_an_action_the_reason_names_but_never_shows_ends_as_a_gap(self):
        self.capable()
        self.provider.records[("thread", 2)] = self.page_of([], False)
        self.discover(reason="assign")
        runs = 0
        while self.envelope()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_RECORD_ATTEMPTS)
            self.enrich()
        self.assertEqual(runs, ENRICH_RECORD_ATTEMPTS - 1)
        self.assertIn({"scope": "notification", "code": "notification_reason_unconfirmed", "need": "action_record",
                       "reason": "assign"}, self.envelope()["message"]["coverage_gaps"])

    def test_a_comment_the_notification_points_to_is_read_again_when_missing(self):
        self.capable()
        late = note(950, OTHER, "@contributor here", "2026-02-02T00:00:00Z")
        waiting = self.discover(latest_comment_url="https://api.github.com/repos/example/project/issues/comments/950")
        self.assertEqual((waiting["_state"], self.items()), ("enriching", []))
        self.provider.rows = [pull(), late]
        self.assertTrue(self.enrich())
        item, = self.items()
        self.assertEqual(item["message"]["message_id"], "IC_950")
        self.assertEqual(self.envelope()["_state"], "ready")


class ActionIdentityTests(OutsideCase):
    """An assignment or review request is GitHub's own timeline event, by its node id: never merged by time or actor."""

    def notify(self, number, ident, recipient=22, updated="2026-02-02T00:00:01Z"):
        url = "https://api.github.com/repos/example/project/pulls/" + str(number)
        self.provider.pages[url] = Response(200, pull(id=200 + number, node_id="PR_20" + str(number), number=number), {})
        notification = {**self.notification(recipient, updated=updated), "id": ident,
                        "subject": {"type": "PullRequest", "url": url}}
        stream = "notifications:" + str(recipient) + ":children"
        with self.store.transaction() as db:
            self.store.job(db, stream, {"kind": "notification_discovery", "recipient": recipient, "notification": notification,
                                        "previous_updated_at": "2026-02-01T00:00:00Z"})
        self.assertTrue(self.tick_only(stream))

    def request(self, number, ident, at="2026-02-02T00:00:00Z", reviewer=HUMAN):
        self.provider.records[("thread", number)] = {"events": [{"type": "ReviewRequestedEvent", "id": ident, "created_at": at,
                                                                 "actor": gql(OTHER), "reviewer": target(reviewer)}]}

    def test_requests_in_two_pull_requests_in_the_same_second_stay_two_events(self):
        self.capable()
        self.request(2, "RRE_20")
        self.request(3, "RRE_30")
        self.notify(2, "N-pr2")
        self.notify(3, "N-pr3")
        found = [item for item in self.items() if item["facts"] == ["review_requested"]]
        self.assertEqual(sorted(item["message"]["raw"]["id"] for item in found), ["RRE_20", "RRE_30"])
        self.assertEqual({item["thread_id"] for item in found}, {"pr.2", "pr.3"})
        self.assertEqual(len({item["source_event_id"] for item in found}), 2)

    def test_one_timeline_event_seen_by_both_readers_is_one_event(self):
        self.capable()
        self.request(2, "RRE_21", reviewer=WRITER)
        self.notify(2, "N-a", recipient=22)
        self.notify(2, "N-b", recipient=11)
        found, = [item for item in self.items() if item["facts"] == ["review_requested"]]
        self.assertEqual(found["message"]["fact_evidence"]["review_requested"][0]["event_id"], "RRE_21")
        for recipient in (22, 11):
            self.assertEqual(self.envelope(recipient)["message"]["fact_evidence"]["items"], [found["source_event_id"]])

    def test_a_timeline_event_without_its_node_id_is_a_gap_not_a_merged_guess(self):
        self.capable()
        self.provider.records[("thread", 2)] = {"events": [{"type": "AssignedEvent", "created_at": "2026-02-02T00:00:00Z",
                                                            "actor": gql(OTHER), "assignee": target(HUMAN)}]}
        self.notify(2, "N-c")
        self.assertEqual(self.items(), [])
        self.assertIn("addressed_action_unaddressable", [g["code"] for g in self.envelope()["message"]["coverage_gaps"]])


class LaterTimelinePageFailureTests(OutsideCase):
    def test_a_failed_later_page_keeps_the_action_an_earlier_page_proved(self):
        self.capable()
        first = {"events": [{"type": "AssignedEvent", "id": "AE_7", "created_at": "2026-02-02T00:00:00Z", "actor": gql(OTHER),
                             "assignee": target(HUMAN)}], "edits": [], "events_incomplete": True, "events_cursor": "cursor-1"}
        self.provider.records[("thread", 2)] = [first, unavailable()] * (ENRICH_ATTEMPTS + 1)
        waiting = self.discover()
        item, = self.items()
        offered = dump(item)
        self.assertEqual((item["_state"], item["facts"], item["actor"]["platform_actor_id"]), ("ready", ["assigned"], "33"))
        self.assertEqual(waiting["_state"], "enriching")  # the rest of the timeline is read again, boundedly
        runs = 0
        while self.envelope()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.enrich()
        done = self.envelope()
        self.assertIn({"scope": "notification", "code": "addressed_action_unverified", "need": "thread_record",
                       "error": "provider_http_502"}, done["message"]["coverage_gaps"])
        kept, = self.items()
        self.assertEqual(dump(kept), offered)
        self.assertEqual(done["message"]["fact_evidence"]["items"], [item["source_event_id"]])


class EmptyEditRecordTests(OutsideCase):
    """A comment GitHub says was updated after it was written, whose edit record does not show the edit."""

    def setup_comment(self, records):
        row = note(960, OTHER, "@contributor please review", "2026-02-02T00:00:00Z", "2026-02-02T00:00:30Z")
        self.provider.rows = [pull(), row]
        self.provider.records[("node", "IC_960")] = records

    def drain(self):
        runs = 0
        while self.envelope()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.enrich()
        return runs

    def test_an_empty_or_incomplete_record_never_makes_the_words_their_first_authors(self):
        self.capable()
        for record in ({"edits": [], "last_edited_at": None}, {"edits": [], "edits_incomplete": True}):
            with self.subTest(record=record):
                with self.store.transaction() as db:
                    db.execute("DELETE FROM snapshots")
                    db.execute("DELETE FROM jobs")
                    db.execute("DELETE FROM inbox WHERE id!='event-1'")
                self.setup_comment(record)
                waiting = self.discover(updated="2026-02-02T00:00:31Z")
                self.assertEqual((waiting["_state"], self.items()), ("enriching", []))  # reread before deciding
                self.assertEqual(self.drain(), ENRICH_RECORD_ATTEMPTS - 1)
                item, = self.mention_events()
                self.assertEqual(item["actor"], {"platform": "github", "attribution": "unknown"})
                self.assertTrue(core_reads_as_change(item["message"]))
                self.assertIn({"scope": "actor", "code": "change_record_missing"}, item["message"]["coverage_gaps"])
                self.assertEqual(item["message"]["forward_origin"]["sender_user"]["id"], "33")

    def test_a_record_that_appears_on_reread_attributes_the_words_normally(self):
        self.capable()
        self.setup_comment([{"edits": []}, {"edits": [revision("2026-02-02T00:00:30Z", OTHER),
                                                       creation("2026-02-02T00:00:00Z", OTHER)]}])
        self.discover(updated="2026-02-02T00:00:31Z")
        self.assertEqual(self.drain(), 1)
        item, = self.mention_events()
        self.assertEqual(item["actor"]["platform_actor_id"], "33")
        self.assertFalse(core_reads_as_change(item["message"]))


class UnresolvedActorTests(OutsideCase):
    """An account GitHub names without a number (GraphQL ``databaseId`` is nullable) whose lookup keeps failing:
    only its own item waits, for the existing bounded retry, and then goes out with its actor unknown."""

    def setUp(self):
        super().setUp()
        self.provider.pages["/users/lookup-user"] = unavailable()

    def drain(self):
        runs = 0
        while self.envelope()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.enrich()
        return runs

    def test_an_action_whose_actor_keeps_failing_never_holds_back_the_next_proven_action(self):
        self.capable()
        self.provider.records[("thread", 2)] = {"events": [
            {"type": "AssignedEvent", "id": "AE_A", "created_at": "2026-02-02T00:00:00Z",
             "actor": {"login": "lookup-user", "__typename": "User"}, "assignee": target(HUMAN)},
            {"type": "ReviewRequestedEvent", "id": "RRE_B", "created_at": "2026-02-02T00:00:01Z", "actor": gql(OTHER),
             "reviewer": target(HUMAN)}]}
        waiting = self.discover()
        first, = self.items()  # B goes out at once, while A waits for its actor
        offered = dump(first)
        self.assertEqual((first["message"]["raw"]["id"], first["_state"], first["actor"]["platform_actor_id"]), ("RRE_B", "ready", "33"))
        self.assertEqual(waiting["_state"], "enriching")
        self.assertEqual(self.drain(), ENRICH_ATTEMPTS - 1)
        self.assertEqual(len(self.items()), 2)  # each action exactly once
        items = {item["message"]["raw"]["id"]: item for item in self.items()}
        self.assertEqual(sorted(items), ["AE_A", "RRE_B"])
        self.assertEqual(dump(items["RRE_B"]), offered)  # never rewritten
        late = items["AE_A"]
        self.assertEqual(late["actor"], {"platform": "github", "username": "lookup-user", "attribution": "unknown"})
        self.assertEqual(late["facts"], ["assigned"])
        self.assertTrue(core_reads_as_change(late["message"]))  # an unknown change: background, never a wake
        self.assertIn({"scope": "actor", "code": "provider_http_502", "need": "actor:AE_A"}, late["message"]["coverage_gaps"])
        done = self.envelope()
        self.assertEqual(done["_state"], "ready")
        self.assertEqual(sorted(done["message"]["fact_evidence"]["items"]), sorted(i["source_event_id"] for i in items.values()))

    def test_new_words_whose_author_keeps_failing_go_out_as_ordinary_words_after_the_retries(self):
        self.capable()
        nameless = {**note(970, None, "@contributor a question", "2026-02-02T00:00:00Z"),
                    "user": {"login": "lookup-user", "type": "User"}}
        self.provider.rows = [pull(), nameless, note(971, OTHER, "@contributor another", "2026-02-02T00:00:01Z")]
        self.discover()
        known, = self.mention_events()
        self.assertEqual(known["message"]["message_id"], "IC_971")  # the other proven words go out at once
        self.assertEqual(self.drain(), ENRICH_ATTEMPTS - 1)
        late, = [item for item in self.mention_events() if item["message"]["message_id"] == "IC_970"]
        self.assertEqual(late["actor"], {"platform": "github", "username": "lookup-user", "attribution": "unknown"})
        self.assertFalse(core_reads_as_change(late["message"]))  # new words by an unresolved author: ordinary handling
        self.assertIn({"scope": "actor", "code": "provider_http_502", "need": "author:IC_970"}, late["message"]["coverage_gaps"])
        self.assertEqual(len(self.mention_events()), 2)


class KeptProofTests(OutsideCase):
    """What an attempt of an outside proof did read stays with the waiting notification: its thread, its records
    and the items it proved."""

    def drain(self):
        runs = 0
        while self.envelope()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.enrich()
        return runs

    def test_a_comment_read_on_a_later_attempt_stays_read_while_its_author_is_looked_up(self):
        self.capable()
        self.provider.pages["/users/lookup-user"] = unavailable()
        late = {**note(951, None, "@contributor here", "2026-02-02T00:00:00Z"), "user": {"login": "lookup-user", "type": "User"}}
        waiting = self.discover(latest_comment_url="https://api.github.com/repos/example/project/issues/comments/951")
        self.assertEqual((waiting["_state"], self.items()), ("enriching", []))
        self.provider.rows = [pull(), late]
        self.assertFalse(self.enrich())  # attempt 2: the comment is read; its author is not resolved yet
        saved = self.store.meta("enrich:" + waiting["source_event_id"])
        self.assertEqual([need["need"] for need in saved["needs"]], ["author:IC_951"])
        self.assertIn(951, [row.get("id") for row in saved["kwargs"]["history"]])
        self.provider.pages["/users/lookup-user"] = Response(200, {"id": 77, "login": "lookup-user", "type": "User"}, {})
        self.assertTrue(self.enrich())  # attempt 3 proves it from the thread attempt 2 read
        item, = self.mention_events()
        self.assertEqual((item["message"]["message_id"], item["actor"]["platform_actor_id"]), ("IC_951", "77"))
        done = self.envelope()
        self.assertEqual(done["message"]["fact_evidence"]["items"], [item["source_event_id"]])
        self.assertNotIn("latest_comment_unavailable", [gap["code"] for gap in done["message"]["coverage_gaps"]])
        self.assertIn(951, [row.get("id") for row in done["conversation"]["history"]["rows"]])

    def test_an_action_already_read_is_not_lost_when_its_record_becomes_unavailable(self):
        self.capable()
        self.provider.pages["/users/lookup-user"] = unavailable()
        self.provider.records[("thread", 2)] = [{"events": [
            {"type": "AssignedEvent", "id": "AE_K", "created_at": "2026-02-02T00:00:00Z",
             "actor": {"login": "lookup-user", "__typename": "User"}, "assignee": target(HUMAN)}]}] + [unavailable()] * 10
        waiting = self.discover()
        self.assertEqual((waiting["_state"], self.items()), ("enriching", []))  # only its actor is still being read
        self.assertEqual(self.drain(), ENRICH_ATTEMPTS - 1)
        item, = self.items()
        self.assertEqual((item["facts"], item["message"]["raw"]["id"]), (["assigned"], "AE_K"))
        self.assertEqual(item["actor"], {"platform": "github", "username": "lookup-user", "attribution": "unknown"})
        self.assertIn({"scope": "actor", "code": "provider_http_502", "need": "actor:AE_K"}, item["message"]["coverage_gaps"])
        self.assertEqual(len(self.provider.record_calls), 1)  # the record that answered was not read again
        done = self.envelope()
        self.assertEqual(done["message"]["fact_evidence"]["items"], [item["source_event_id"]])
        self.assertNotIn("addressed_action_unverified", [gap["code"] for gap in done["message"]["coverage_gaps"]])

    def test_an_item_proved_on_an_earlier_attempt_stays_in_the_notifications_evidence(self):
        self.capable()
        self.provider.rows = [pull(), note(952, OTHER, "@contributor look", "2026-02-02T00:00:00Z")]
        self.provider.records[("thread", 2)] = [unavailable()] * (ENRICH_ATTEMPTS + 2)
        self.discover()
        item, = self.mention_events()
        self.assertFalse(self.enrich())
        saved = self.store.meta("enrich:" + self.envelope()["source_event_id"])
        self.assertEqual(saved["kwargs"]["items"], [item["source_event_id"]])
        self.provider.rows = [pull()]  # the comment is gone from a thread read later (the saved read is what counts)
        self.drain()
        self.assertEqual(self.envelope()["message"]["fact_evidence"]["items"], [item["source_event_id"]])


class RetainedAuthorTests(OutsideCase):
    """An author GitHub names by login only (GraphQL ``databaseId`` is nullable): the thread read numbers it while the
    notification waits for another read; a restart empties the lookup cache and the lookup then fails. The number that
    read gave this same message stands, for no other message, and an editor's number never stands in for it."""
    VISITOR = {"id": 77, "login": "lookup-user", "type": "User"}
    EDITED = {"edits": [revision("2026-02-02T00:00:30Z", VISITOR), creation("2026-02-02T00:00:00Z", VISITOR)]}

    def setUp(self):
        super().setUp()
        self.capable()
        self.lookups, get = [], self.provider.get

        def counted(path):
            if path.startswith("/users/"):
                self.lookups.append(path)
            return get(path)
        self.provider.get = counted

    def reset(self):
        """A clean state for the next variant of one scenario."""
        with self.store.transaction() as db:
            for table in ("snapshots", "jobs", "notifications"):
                db.execute("DELETE FROM " + table)
            db.execute("DELETE FROM inbox WHERE id!='event-1'")
            db.execute("DELETE FROM meta WHERE key LIKE 'enrich:%'")
        self.provider.records, self.lookups[:] = {}, []
        self.provider.users.clear()

    def restart(self):
        """A new companion process over the same state: no lookup cache, and every account lookup now fails."""
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.outbox.store = self.store
        self.provider.users.clear()
        for path in [path for path in self.provider.pages if path.startswith("/users/")]:
            self.provider.pages[path] = unavailable()
        self.collector = Collector(self.store, self.provider, {**CONFIG, "repositories": []}, {11: self.reader, 22: self.reader})
        self.lookups[:] = []

    def drain(self):
        runs = 0
        while self.envelope()["_state"] == "enriching":
            runs += 1
            self.assertLessEqual(runs, ENRICH_ATTEMPTS)
            self.enrich()
        return runs

    def waiting(self, author, record, numbered=True, **notification):
        """New words naming the reader, written in its window and edited since; ``numbered``: the thread read numbers
        their author by login. The edit record (``record``) is read only after the author is known."""
        self.provider.pages["/users/" + author["login"]] = Response(200, author, {}) if numbered else unavailable()
        row = note(990, {"login": author["login"], "type": "User"}, "@contributor please review", "2026-02-02T00:00:00Z",
                   "2026-02-02T00:00:30Z")
        self.provider.rows = [pull(), row]
        self.provider.records[("node", "IC_990")] = record
        waiting = self.discover(updated="2026-02-02T00:00:31Z", **notification)
        self.assertEqual((waiting["_state"], self.items()), ("enriching", []))
        kept, = [r for r in self.store.meta("enrich:" + waiting["source_event_id"])["kwargs"]["history"] if r["id"] == 990]
        self.assertEqual(kept["_actor_id"], str(author["id"]) if numbered else "")
        return row

    def item(self, message_id="IC_990"):
        found, = [item for item in self.mention_events() if item["message"]["message_id"] == message_id]
        return found

    def test_an_author_first_numbered_on_a_retry_survives_a_failed_edit_record_and_restart(self):
        for author, editor in ((OWNER, OWNER), (self.VISITOR, self.VISITOR), (OWNER, self.VISITOR)):
            for adopted in (False, True):
                with self.subTest(author=author["login"], editor=editor["login"], adopted=adopted):
                    self.reset()
                    record = {"edits": [revision("2026-02-02T00:00:30Z", editor),
                                        creation("2026-02-02T00:00:00Z", author)]}
                    row = self.waiting(author, [unavailable(), record], numbered=False)
                    key = "enrich:" + self.envelope()["source_event_id"]
                    initial = self.store.meta(key)
                    self.assertEqual(initial["needs"], [{"need": "author:IC_990", "code": "provider_http_502"}])
                    if adopted:
                        self.provider.rows = [pull(), {**row, "body": "@contributor newer words"}]
                        newer = self.discover(recipient=11, updated="2026-02-02T00:00:32Z")
                        self.assertEqual(newer["_state"], "enriching")
                        newer_key = "enrich:" + newer["source_event_id"]
                        newer_saved = self.store.meta(newer_key)
                    lookup = "/users/" + author["login"]
                    self.provider.pages[lookup] = Response(200, author, {})
                    self.lookups[:] = []
                    self.assertFalse(self.enrich())  # first successful author lookup, then a failed edit-record read
                    self.assertEqual(self.lookups, [lookup])
                    saved = self.store.meta(key)
                    self.assertEqual(saved["needs"], [{"need": "edits:IC_990", "code": "provider_http_502"}])
                    self.assertEqual(saved["attempts"], 2)
                    self.restart()  # cache gone; every subsequent account lookup fails again
                    runs = self.drain()
                    item = self.item()
                    kept, = [r for r in saved["kwargs"]["history"] if r["id"] == 990]
                    with self.subTest(stage="saved before restart"):
                        self.assertEqual(kept["_actor_id"], str(author["id"]))
                    with self.subTest(stage="after restart"):
                        if author == editor:
                            self.assertEqual(item["actor"].get("platform_actor_id"), str(author["id"]))
                            self.assertFalse(core_reads_as_change(item["message"]))
                            self.assertNotIn("forward_origin", item["message"])
                            self.assertEqual([gap for gap in item["message"]["coverage_gaps"] if gap["scope"] == "actor"], [])
                        else:
                            self.assertEqual(item["actor"], {"platform": "github", "attribution": "unknown"})
                            self.assertTrue(core_reads_as_change(item["message"]))
                            self.assertEqual(item["message"]["forward_origin"]["sender_user"].get("id"), "44")
                            self.assertIn({"scope": "actor", "code": "multiple_change_actors"},
                                          item["message"]["coverage_gaps"])
                        self.assertEqual(runs, 1)
                        self.assertEqual(self.lookups, [lookup])
                    # Enrich only the matching row's number, preserving each read's words and observation time.
                    expected = json.loads(json.dumps(initial["kwargs"]["history"]))
                    next(r for r in expected if r["id"] == 990)["_actor_id"] = str(author["id"])
                    with self.subTest(stage="history custody"):
                        self.assertEqual(saved["kwargs"]["history"], expected)
                        self.assertEqual(saved["kwargs"]["history_at"], initial["kwargs"]["history_at"])
                        if adopted:
                            self.assertEqual(self.store.meta(newer_key), newer_saved)
                            self.assertEqual(saved["superseded"]["holder"], newer["source_event_id"])
                            self.assertEqual(item["message"]["raw"]["body"], "@contributor newer words")
                            self.assertEqual(item["conversation"]["history"]["observed_at"],
                                             newer_saved["kwargs"]["history_at"])

    def test_a_retry_number_never_moves_to_another_message_or_login_in_a_newer_read(self):
        for changed in ("message", "login"):
            with self.subTest(changed=changed):
                self.reset()
                row = self.waiting(OWNER, unavailable(), numbered=False)
                self.provider.pages["/users/owner-account"] = Response(200, OWNER, {})
                self.assertFalse(self.enrich())  # its own row is first numbered here
                self.restart()
                fresh = {**row, "body": "@contributor new words", "updated_at": row["created_at"]}
                if changed == "message":
                    fresh.update(id=991, node_id="IC_991")
                else:
                    fresh["user"] = {"login": self.VISITOR["login"], "type": "User"}
                    self.provider.pages["/users/lookup-user"] = unavailable()
                self.provider.rows = [pull(), fresh]
                newer = self.discover(recipient=11, updated="2026-02-02T00:00:32Z")
                self.assertEqual(newer["_state"], "enriching")
                newer_key = "enrich:" + newer["source_event_id"]
                newer_saved = self.store.meta(newer_key)
                self.assertEqual(self.drain(), ENRICH_ATTEMPTS - 2)
                item = self.item(fresh["node_id"])
                self.assertEqual(item["actor"], {"platform": "github", "username": fresh["user"]["login"],
                                                 "attribution": "unknown"})
                self.assertFalse(core_reads_as_change(item["message"]))
                self.assertEqual(item["message"]["raw"]["body"], fresh["body"])
                self.assertEqual(self.store.meta(newer_key), newer_saved)
                self.assertEqual(len(self.mention_events()), 1)

    def test_the_number_the_thread_read_gave_an_author_stands_when_its_lookup_fails_after_a_restart(self):
        for author in (self.VISITOR, OWNER):
            with self.subTest(author=author["login"]):
                self.reset()
                self.waiting(author, [unavailable(), {"edits": [revision("2026-02-02T00:00:30Z", author),
                                                                creation("2026-02-02T00:00:00Z", author)]}])
                self.restart()
                runs = self.drain()
                item = self.item()
                self.assertEqual(item["actor"].get("platform_actor_id"), str(author["id"]))
                self.assertEqual((item["facts"], item["message"]["action"]), (["mention"], "observed"))
                self.assertFalse(core_reads_as_change(item["message"]))  # its author's own new words
                self.assertNotIn("forward_origin", item["message"])
                self.assertEqual([gap for gap in item["message"]["coverage_gaps"] if gap["scope"] == "actor"], [])
                self.assertEqual(runs, 1)  # proven at the retry its record answers, never held for its author
                self.assertEqual(self.lookups, ["/users/" + author["login"]])  # the one lookup that failed, no other

    def test_another_editor_or_an_author_never_numbered_stays_as_it_was(self):
        foreign = {"edits": [revision("2026-02-02T00:00:30Z", OTHER), creation("2026-02-02T00:00:00Z", self.VISITOR)]}
        for case, record, numbered, runs in (("another account edited", [unavailable(), foreign], True, 1),
                                             ("author never numbered", self.EDITED, False, ENRICH_ATTEMPTS - 1)):
            with self.subTest(case=case):
                self.reset()
                self.waiting(self.VISITOR, record, numbered)
                self.restart()
                drained = self.drain()
                item = self.item()
                self.assertEqual(item["actor"], {"platform": "github", "attribution": "unknown"})
                self.assertTrue(core_reads_as_change(item["message"]))  # an unattributed change: background
                origin, gaps = item["message"]["forward_origin"]["sender_user"], item["message"]["coverage_gaps"]
                if numbered:  # the words' first author as the thread read numbered it; the other editor stays other
                    self.assertEqual((origin.get("id"), origin["username"]), ("77", "lookup-user"))
                    self.assertIn({"scope": "actor", "code": "multiple_change_actors"}, gaps)
                else:  # never numbered: the editor's number is not taken for the author
                    self.assertEqual((origin.get("id"), origin["username"]), (None, "lookup-user"))
                    self.assertIn({"scope": "actor", "code": "provider_http_502", "need": "author:IC_990"}, gaps)
                self.assertEqual(drained, runs)

    def test_an_edit_adding_the_mention_by_the_words_own_author_stays_theirs(self):
        written = "2026-02-02T00:00:00Z"
        self.provider.pages["/users/owner-account"] = Response(200, OWNER, {})
        row = note(992, {"login": "owner-account", "type": "User"}, "please review", written)
        self.provider.rows = [pull(), row]
        first = between_reads()
        self.assertEqual(self.discover(updated=first)["_state"], "ready")  # read without a mention
        edited = between_reads()
        self.provider.rows = [pull(), {**row, "body": "@contributor please review", "updated_at": edited}]
        self.provider.records[("node", "IC_992")] = [unavailable(), {"edits": [revision(edited, OWNER), creation(written, OWNER)]}]
        self.assertEqual(self.discover(updated=between_reads(), previous=first)["_state"], "enriching")
        self.restart()
        runs = self.drain()
        item = self.item("IC_992")
        self.assertEqual((item["actor"]["platform_actor_id"], item["message"]["action"]), ("44", "edited"))
        self.assertEqual(item["message"]["original_author"].get("platform_actor_id"), "44")
        self.assertNotIn("forward_origin", item["message"])  # the editor's own words, not copied ones
        self.assertEqual(self.non_text(item, "change_attribution"),
                         ["GitHub records @owner-account (id 44) as having made this change."])
        self.assertEqual(runs, 1)

    def test_a_later_read_of_the_thread_keeps_the_number_for_the_same_message_only(self):
        later = note(991, {"login": "lookup-user", "type": "User"}, "@contributor one more", "2026-02-02T00:00:10Z")
        pointer = {"latest_comment_url": "https://api.github.com/repos/example/project/issues/comments/991"}
        for how in ("its own reread", "another reader's newer read"):
            with self.subTest(how=how):
                self.reset()
                own = how == "its own reread"
                row = self.waiting(self.VISITOR, [unavailable(), self.EDITED], **(pointer if own else {}))
                self.restart()
                self.provider.rows = [pull(), row, later]  # the same login on another message: never numbered
                if not own:
                    self.assertEqual(self.discover(recipient=11, updated="2026-02-02T00:00:32Z")["_state"], "enriching")
                self.assertEqual(self.drain(), ENRICH_ATTEMPTS - 1)  # the other message's author keeps failing
                item = self.item()
                self.assertEqual(item["actor"].get("platform_actor_id"), "77")
                self.assertFalse(core_reads_as_change(item["message"]))
                other = self.item("IC_991")
                self.assertEqual(other["actor"], {"platform": "github", "username": "lookup-user", "attribution": "unknown"})
                self.assertFalse(core_reads_as_change(other["message"]))  # new words by an unresolved author
                self.assertEqual(len(self.mention_events()), 2)


class HostDowngradeTests(AttentionCase):
    def change(self, eid):
        value = json.loads(self.store.one("SELECT event FROM inbox WHERE id='event-1'")["event"])
        value.update(source_event_id=eid, facts=[], text="")
        value["message"] = {**value["message"], "non_text": [{"kind": "state_change", "description": "Closed the issue."}]}
        self.add_event(value)
        return value

    def dispatch(self):
        sent = []
        host = Host(SimpleNamespace(request=lambda method, path, payload: sent.append(payload) or Response(
            200, {"disposition": "retry"}, {})), self.store, self.outbox)
        host.dispatch()
        host.close()
        return sent

    def test_a_queued_change_waits_unchanged_for_a_host_that_reads_changes(self):
        self.capable(change=False)
        self.change("change-1")
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET state='ready' WHERE id='event-1'")
        sent = self.dispatch()
        self.assertEqual([p["event"]["source_event_id"] for p in sent], ["event-1"])  # the 0.1.6 form still goes
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET state='accepted' WHERE id='event-1'")
        held = self.store.one("SELECT state,event FROM inbox WHERE id='change-1'")
        self.assertEqual(held["state"], "blocked")
        gap = self.store.one("SELECT code,detail FROM gaps WHERE scope='intake:change-1' AND resolved IS NULL")
        self.assertEqual((gap["code"], json.loads(gap["detail"])), ("host_capability_missing", {"requires": ["presence_change_context"]}))
        self.assertIsNone(self.store.meta("dispatch:change-1"))  # nothing frozen or sent narrower meanwhile
        self.capable(change=True)
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET due=0")
        sent = self.dispatch()
        self.assertEqual([p["event"]["source_event_id"] for p in sent], ["change-1"])
        self.assertEqual(sent[0]["event"]["facts"], [])
        self.assertEqual(json.loads(held["event"])["message"]["non_text"], sent[0]["event"]["message"]["non_text"])

    def test_an_offered_change_keeps_its_frozen_payload_across_a_downgrade(self):
        self.capable(change=True)
        self.change("change-2")
        first = self.dispatch()
        self.assertEqual([p["event"]["source_event_id"] for p in first], ["change-2"])
        frozen = self.store.meta("dispatch:change-2")
        self.capable(change=False)
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET due=0")
        self.assertEqual(self.dispatch(), [])
        self.assertEqual(self.store.one("SELECT state FROM inbox WHERE id='change-2'")["state"], "blocked")
        self.capable(change=True)
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET due=0")
        again = self.dispatch()
        self.assertEqual(again, [frozen])
        self.assertEqual(self.store.meta("dispatch:change-2"), frozen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
