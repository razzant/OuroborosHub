"""Offline action custody and recovery tests; no Telegram login or network."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import userbot.action_store as module
from userbot.action_store import ActionStore, validate_history_query


class ActionStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "private" / "actions.db"
        self.now = [1000.0]
        self.clock = lambda: self.now[0]
        self.digest = hashlib.sha256(b"private content not in DB").hexdigest()

    def store(self, *, recover_attempted=False):
        result = ActionStore(self.path, clock=self.clock,
                             recover_attempted=recover_attempted)
        self.addCleanup(result.close)
        return result

    def edit(self, store, account="a", intent="one", **changes):
        args = dict(operation="edit", peer_id=-10022, binding_id="b" * 32,
                    topic_id=17, message_id=42,
                    payload_ref="payload:hash-ref", payload_sha256=self.digest)
        args.update(changes)
        return store.enqueue(account, intent, **args)

    def test_idempotency_freezes_every_input_and_account(self):
        store = self.store()
        first = self.edit(store)
        self.assertEqual("queued", first.status)
        self.assertEqual(first, self.edit(store))
        self.assertEqual(first, store.get("a", "one"))
        self.assertIsNone(store.get("b", "one"))
        self.assertEqual("queued", self.edit(store, "b").status)
        for difference in (
            {"operation": "react"}, {"peer_id": -10023}, {"topic_id": None},
            {"message_id": 43}, {"payload_ref": "payload:other"},
            {"payload_sha256": "0" * 64}, {"binding_id": "c" * 32},
        ):
            with self.subTest(difference=difference), self.assertRaises(ValueError):
                self.edit(store, **difference)
        store.close()
        self.assertEqual(first, self.store().get("a", "one"))

    def test_no_private_payload_in_database_and_invalid_shapes(self):
        store = self.store()
        self.edit(store)
        raw = b"".join(p.read_bytes() for p in
                       (self.path, Path(str(self.path) + "-wal")) if p.exists())
        self.assertNotIn(b"private content not in DB", raw)
        self.assertIn(self.digest.encode(), raw)
        with self.assertRaises(ValueError):
            self.edit(store, payload_ref="private content not in DB")
        with self.assertRaises(ValueError):
            self.edit(store, payload_sha256="invalid")
        for invalid in ({"operation": "history"}, {"peer_id": 0},
                        {"message_id": 2**31}, {"topic_id": True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.edit(store, "a", "different", **invalid)
        deleted = store.enqueue("a", "delete", operation="delete", peer_id=-10022,
                                binding_id="b" * 32, topic_id=None, message_id=42)
        self.assertIsNone(deleted.payload_ref)
        with self.assertRaises(ValueError):
            store.enqueue("a", "bad-delete", operation="delete", peer_id=-10022,
                          binding_id="b" * 32, topic_id=None, message_id=42, payload_ref="data:1")
        with self.assertRaises(ValueError):
            ActionStore("relative.db")
        with self.assertRaises(ValueError):
            ActionStore(Path(module.__file__).resolve().parent.parent / "forbidden.db")

    def test_fifo_preflight_reject_not_poison_next_action(self):
        store = self.store()
        self.edit(store)
        store.enqueue("a", "second", operation="delete", peer_id=-10022,
                      binding_id="b" * 32, topic_id=18, message_id=43)
        first = store.claim("a")
        self.assertEqual("one", first.intent_id)
        with self.assertRaises(ValueError):
            store.mark_attempted("a", "one", "wrong-token")
        self.assertEqual("failed", store.reject_before_attempt(
            "a", "one", first.lease_token, "topic_removed").status)
        second = store.claim("a")
        self.assertEqual("second", second.intent_id)
        self.assertEqual(18, second.topic_id)
        self.assertEqual("attempted", store.mark_attempted(
            "a", "second", second.lease_token).status)
        self.assertEqual("sent", store.settle(
            "a", "second", second.lease_token, success=True).status)
        self.assertIsNone(store.claim("a"))
        with self.assertRaises(ValueError):
            store.mark_attempted("a", "one", first.lease_token)

    def test_reclaim_only_unattempted_lease_fences_stale_token(self):
        store = self.store()
        self.edit(store)
        original = store.claim("a", lease_seconds=5)
        self.assertIsNone(store.claim("a"))
        self.now[0] += 6
        later = store.claim("a")
        self.assertNotEqual(original.lease_token, later.lease_token)
        with self.assertRaises(ValueError):
            store.mark_attempted("a", "one", original.lease_token)
        with self.assertRaises(ValueError):
            store.reject_before_attempt("a", "one", original.lease_token, "invalid_topic")
        store.mark_attempted("a", "one", later.lease_token)
        self.now[0] += 61
        self.assertEqual("unknown", store.list_unknown("a")[0].status)
        self.assertIsNone(store.claim("a"))
        with self.assertRaises(ValueError):
            store.settle("a", "one", original.lease_token, success=True)
        self.assertEqual("sent", store.settle("a", "one", later.lease_token,
                                               success=True).status)

    def test_attempted_crash_never_auto_retry_and_uncertain_result(self):
        store = self.store()
        self.edit(store)
        claimed = store.claim("a")
        store.mark_attempted("a", "one", claimed.lease_token)
        observer = self.store()
        self.assertEqual("attempted", observer.get("a", "one").status)
        observer.close()
        store.close()
        recovered = self.store(recover_attempted=True)
        self.assertEqual("unknown", recovered.get("a", "one").status)
        self.assertEqual([recovered.get("a", "one")], recovered.list_unknown("a"))
        self.assertEqual([], recovered.list_unknown("b"))
        self.assertIsNone(recovered.claim("a"))
        with self.assertRaises(ValueError):
            recovered.mark_attempted("a", "one", claimed.lease_token)
        self.assertEqual("failed", recovered.settle("a", "one", claimed.lease_token,
                                                      success=False, error_code="rpc_refused").status)
        self.assertEqual([], recovered.list_unknown("a"))

    def test_ambiguous_and_definitive_outcomes_are_distinct(self):
        store = self.store()
        self.edit(store)
        first = store.claim("a")
        with self.assertRaises(ValueError):
            store.mark_unknown("a", "one", first.lease_token)
        store.mark_attempted("a", "one", first.lease_token)
        self.assertEqual("unknown", store.mark_unknown("a", "one", first.lease_token).status)
        self.assertIsNone(store.claim("a"))
        with self.assertRaises(ValueError):
            store.settle("a", "one", first.lease_token, success=False,
                         error_code="private error: phone number!")
        self.assertEqual("unknown", store.get("a", "one").status)
        self.assertEqual("sent", store.settle("a", "one", first.lease_token,
                                               success=True).status)
        with self.assertRaises(ValueError):
            store.settle("a", "one", first.lease_token, success=True)
        self.assertEqual("sent", self.edit(store).status)

    def test_release_transient_preflight_and_independent_accounts(self):
        store = self.store()
        self.edit(store)
        self.edit(store, "b")
        first = store.claim("a")
        assert first is not None
        self.assertEqual("queued", store.release_before_attempt(
            "a", "one", first.lease_token).status)
        self.assertEqual("leased", store.claim("b").status)
        again = store.claim("a")
        self.assertNotEqual(first.lease_token, again.lease_token)
        with self.assertRaises(ValueError):
            store.mark_attempted("a", "one", first.lease_token)

    def test_history_is_bounded_read_not_an_action(self):
        query = validate_history_query(-10022, topic_id=None, limit=100, min_id=42)
        self.assertEqual((-10022, None, 100, 42),
                         (query.peer_id, query.topic_id, query.limit, query.min_id))
        for options in ({"limit": 101}, {"limit": 0}, {"limit": True},
                        {"min_id": -1}, {"topic_id": 2**31}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_history_query(-10022, **{"topic_id": None, **options})
        store = self.store()
        self.assertIsNone(store.claim("a"))
        self.assertEqual(0, store.db.execute("SELECT count(*) FROM actions").fetchone()[0])
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        self.assertEqual(0, db.execute("SELECT count(*) FROM actions").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
