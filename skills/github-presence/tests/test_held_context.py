"""Wire custody, room isolation and restart controls for next-author discovery."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_transport import Case, CONFIG, Host, Outbox, Response, Store, event


class HeldContextTests(Case):
    def held(self):
        self.send_tool()
        self.provider.rows.append({"id": 1555, "body": "Changed context"})
        self.advance()
        self.assertEqual(self.store.one("SELECT state FROM outbox")["state"], "needs_reconsideration")
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET state='accepted'")

    def dispatch(self, *, response=None):
        seen = []
        def request(method, path, payload):
            seen.append(copy.deepcopy(payload))
            return Response(200, response or {"status": "completed", "outcome": "silent"}, {})
        host = Host(SimpleNamespace(request=request), self.store, self.outbox)
        host.dispatch()
        host.close()
        return seen

    def test_next_genuine_event_has_prior_held_intent_without_changing_provider_custody(self):
        self.held()
        next_event = event("next-provider-event")
        self.add_event(next_event)
        original = self.store.rows("SELECT id,event FROM inbox ORDER BY seq")
        sent, = self.dispatch()
        queue = sent["event"]["conversation"].pop("transport_queue")
        self.assertEqual(sent["event"], next_event)
        self.assertEqual(queue["after_source_event_id"], "next-provider-event")
        held, = queue["held_intents"]
        self.assertEqual((held["delivery_id"], held["source_event_id"]), ("tool:request-1", "event-1"))
        self.assertEqual(queue["events"], [])
        source = json.loads(Path(sent["staged_files"][0]).read_text())
        self.assertEqual(source["transport_queue"], queue)
        self.assertEqual(source["authority"], "context_only")
        self.assertEqual(source["attribution"], "unknown")
        self.assertEqual(self.store.rows("SELECT id,event FROM inbox ORDER BY seq"), original)
        self.assertFalse(self.provider.posts)

    def test_other_thread_repository_and_account_cannot_inherit_held_intent(self):
        self.held()
        for field, value in (("thread_id", "issue.2"), ("conversation_id", "repo.999"), ("account_id", "other-account")):
            other = event("other-" + field)
            other[field] = value
            other["conversation_key"] = ":".join(other[k] for k in ("provider", "account_id", "conversation_id", "thread_id"))
            self.add_event(other)
        for sent in self.dispatch():
            self.assertEqual(sent["staged_files"], [])
            self.assertNotIn("transport_queue", sent["event"]["conversation"])

    def test_retry_after_restart_freezes_wire_and_source_even_when_outbox_changes(self):
        self.held()
        self.add_event(event("next-provider-event"))
        first, = self.dispatch(response={"disposition": "retry", "code": "allowance_exhausted"})
        attachment = Path(first["staged_files"][0])
        raw = attachment.read_bytes()
        attachment.unlink()  # source is rebuilt from frozen bytes, never today's queue
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET state='delivered',detail='{}'")
            db.execute("UPDATE inbox SET state='submitting',due=0 WHERE id='next-provider-event'")
        self.store.close()
        self.store = Store(self.scratch.name)
        self.store.recover()
        self.outbox = Outbox(self.store, CONFIG)
        retry, = self.dispatch()
        self.assertEqual(retry, first)
        self.assertEqual(attachment.read_bytes(), raw)

    def test_empty_first_snapshot_and_legacy_response_stay_exact_on_retry(self):
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET state='accepted'")
        self.add_event(event("next-provider-event"))
        first, = self.dispatch(response={"disposition": "retry"})
        self.assertEqual(first["staged_files"], [])
        self.held()
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET state='ready',due=0 WHERE id='next-provider-event'")
        retry, = self.dispatch()
        self.assertEqual(retry, first)
        original = event("legacy-retry")
        self.add_event(original)
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET response='{}' WHERE id='legacy-retry'")
        legacy, = self.dispatch()
        self.assertEqual(legacy["staged_files"], [])
        self.assertEqual(legacy["event"], original)

    def test_context_publication_closes_temporary_handle_before_replace(self):
        import os
        import tempfile
        self.held()
        self.add_event(event("next-provider-event"))
        opened = []
        create, replace = tempfile.NamedTemporaryFile, os.replace
        def tracked_file(*args, **kwargs):
            file = create(*args, **kwargs)
            opened.append(file)
            return file
        def replace_after_close(source, target):
            self.assertTrue(opened and all(file.closed for file in opened))
            return replace(source, target)
        with patch("gh_presence.host.tempfile.NamedTemporaryFile", tracked_file), patch("gh_presence.host.os.replace", replace_after_close):
            sent, = self.dispatch()
        self.assertTrue(Path(sent["staged_files"][0]).is_file())

    def test_unreadable_source_blocks_intake_instead_of_sending_without_context(self):
        self.held()
        self.add_event(event("next-provider-event"))
        with patch.object(Host, "stage_context", side_effect=OSError("fixture disk failure")):
            self.assertEqual(self.dispatch(), [])
        self.assertEqual(self.store.one("SELECT state FROM inbox WHERE id='next-provider-event'")["state"], "blocked")
        self.assertEqual(self.store.one("SELECT code FROM gaps WHERE scope='intake:next-provider-event'")["code"], "held_context_source_unavailable")
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET due=0 WHERE id='next-provider-event'")
        retried, = self.dispatch()
        self.assertTrue(retried["staged_files"])

    def test_next_author_queue_updates_include_old_source_intents(self):
        self.held()
        self.add_event(event("next-provider-event"))
        seen = []
        host = Host(SimpleNamespace(request=lambda *args: seen.append(args) or Response(200, {"status": "recorded"}, {})), self.store, self.outbox)
        self.addCleanup(host.close)
        host.observe_queue({"ref": "next-author", "event_id": "next-provider-event"})
        self.assertEqual(seen[0][1], "/presence/work/next-author")
        self.assertEqual(seen[0][2]["transport_queue"]["held_intents"][0]["source_event_id"], "event-1")
