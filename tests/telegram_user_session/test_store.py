"""Crash/reopen and account-isolation tests for the durable Telethon send boundary."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import userbot.store as store_module
from userbot.store import DurableStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "jobs.sqlite3"
        self.now = [1_000.0]
        self.clock = lambda: self.now[0]

    def store(self, *, recover_attempted=False):
        store = DurableStore(self.path, clock=self.clock,
                             recover_attempted=recover_attempted)
        self.addCleanup(store.close)
        return store

    def test_new_and_edit_deduplication_account_scope(self):
        store = self.store()
        self.assertTrue(store.record_inbox("a", -1001, 5))
        self.assertFalse(store.record_inbox("a", -1001, 5))
        self.assertTrue(store.record_inbox("a", -1001, 5, kind="edit", version="2026-10-12T20:00:00.123456Z"))
        self.assertFalse(store.record_inbox("a", -1001, 5, kind="edit", version="2026-10-12T20:00:00.123456Z"))
        self.assertTrue(store.record_inbox("a", -1001, 5, kind="edit", version="2026-10-12T20:00:01.123456Z"))
        self.assertTrue(store.record_inbox("b", -1001, 5))
        self.assertTrue(store.record_inbox("a", -1002, 5))
        with self.assertRaises(ValueError):
            store.record_inbox("a", -1001, 5, kind="edit")
        store.close()
        self.assertFalse(self.store().record_inbox("a", -1001, 5))

    def test_inbound_payload_survives_crash_until_host_ack(self):
        store = self.store()
        event_id = "a:-1001:topic17:message5:edit2026-10-12T20:00:00Z"
        payload = {
            "source_event_id": event_id, "account_id": "a", "provider": "telegram",
            "conversation_id": "-1001", "thread_id": "17",
            "conversation_key": "opaque:key", "conversation": {"id": "-1001"},
            "message": {"id": 5}, "text": "private привет",
            "actor": {"id": "someone"},
        }
        reserved = store.reserve_inbox("a", event_id, payload)
        self.assertEqual("pending", reserved.status)
        self.assertEqual([reserved], store.pending_inbox("a"))
        self.assertEqual([], store.pending_inbox("b"))
        store.close()  # die before Host delivery
        reopened = self.store()
        recovered = reopened.pending_inbox("a")[0]
        self.assertEqual(reserved.payload_json, recovered.payload_json)
        self.assertEqual(payload, recovered.payload)
        self.assertEqual(reserved, reopened.reserve_inbox("a", event_id, dict(reversed(list(payload.items())))))
        with self.assertRaises(ValueError):
            reopened.reserve_inbox("a", event_id, {**payload, "text": "changed"})
        with self.assertRaises(LookupError):
            reopened.ack_inbox("b", event_id)
        # Die after Host completed, before local ACK: same bytes remain pending
        # so the Host can deduplicate the replayed source_event_id.
        reopened.close()
        final = self.store()
        self.assertEqual([reserved], final.pending_inbox("a"))
        self.assertTrue(final.ack_inbox("a", event_id))
        self.assertFalse(final.ack_inbox("a", event_id))
        self.assertEqual([], final.pending_inbox("a"))
        tombstone = final.reserve_inbox("a", event_id, payload)
        self.assertEqual("acked", tombstone.status)
        self.assertEqual("", tombstone.payload_json)
        self.assertEqual(reserved.payload_digest, tombstone.payload_digest)
        with self.assertRaises(ValueError):
            _ = tombstone.payload
        with self.assertRaises(ValueError):
            final.reserve_inbox("a", event_id, {**payload, "text": "changed after ACK"})
        with sqlite3.connect(self.path) as db:
            self.assertEqual(("", "acked", reserved.payload_digest), db.execute(
                "SELECT payload_json,status,payload_digest FROM inbox_delivery "
                "WHERE account_id=? AND event_id=?", ("a", event_id)).fetchone())
        final.close()
        reopened_acked = self.store()
        self.assertEqual(tombstone, reopened_acked.reserve_inbox("a", event_id, payload))
        self.assertEqual([], reopened_acked.pending_inbox("a"))

    def test_legacy_inbox_migration_scrubs_only_acked_events(self):
        event_id = "a:-1001:topic17:message5"
        def event(key, text):
            return {
                "source_event_id": key, "account_id": "a", "provider": "telegram",
                "conversation_id": "-1001", "thread_id": "17",
                "conversation_key": "key", "conversation": {}, "message": {},
                "actor": {}, "text": text,
            }
        # Construct the original pre-retention schema and rows as an older
        # companion would have left them after crashing before/after Host ACK.
        import json
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE inbox_delivery (
                account_id TEXT NOT NULL, event_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'acked')),
                PRIMARY KEY (account_id, event_id))""")
            for key, status, text in ((event_id, "acked", "old secret"),
                                      (event_id + ":edit", "pending", "pending secret")):
                db.execute("INSERT INTO inbox_delivery VALUES (?,?,?,?)",
                           ("a", key, json.dumps(event(key, text), ensure_ascii=False,
                                                 sort_keys=True, separators=(",", ":")), status))
        store = self.store()
        self.assertEqual("", store.db.execute(
            "SELECT payload_json FROM inbox_delivery WHERE event_id=?", (event_id,)
        ).fetchone()[0])
        pending = store.pending_inbox("a")
        self.assertEqual(["pending secret"], [item.payload["text"] for item in pending])
        self.assertEqual("acked", store.reserve_inbox("a", event_id,
                        event(event_id, "old secret")).status)
        with self.assertRaises(ValueError):
            store.reserve_inbox("a", event_id, event(event_id, "new secret"))
        self.assertTrue(store.ack_inbox("a", event_id + ":edit"))
        self.assertFalse(store.ack_inbox("a", event_id + ":edit"))
        self.assertEqual([], store.pending_inbox("a"))
        store.close()
        self.assertEqual("acked", self.store().reserve_inbox(
            "a", event_id + ":edit", event(event_id + ":edit", "pending secret")).status)

    def test_ack_does_not_scrub_another_accounts_pending_event(self):
        store = self.store()
        base = {
            "source_event_id": "shared", "provider": "telegram",
            "conversation_id": "-1", "thread_id": "", "conversation_key": "key",
            "conversation": {}, "message": {}, "actor": {}, "text": "pending private text",
        }
        store.reserve_inbox("a", "shared", {**base, "account_id": "a"})
        store.reserve_inbox("b", "shared", {**base, "account_id": "b"})
        self.assertTrue(store.ack_inbox("a", "shared"))
        self.assertEqual([], store.pending_inbox("a"))
        self.assertEqual("pending private text", store.pending_inbox("b")[0].payload["text"])

    def test_inbound_reservation_rejects_mismatched_identity_and_invalid_payload(self):
        store = self.store()
        template = {
            "source_event_id": "event:1", "account_id": "a", "provider": "telegram",
            "conversation_id": "-1001", "thread_id": "17", "conversation_key": "key",
            "conversation": {}, "message": {}, "actor": {}, "text": "x",
        }
        with self.assertRaises(ValueError):
            store.reserve_inbox("a", "event:1", {**template, "source_event_id": "event:2"})
        with self.assertRaises(ValueError):
            store.reserve_inbox("a", "event:1", {**template, "account_id": "b"})
        with self.assertRaises(ValueError):
            store.reserve_inbox("a", "event:1", {**template, "text": float("nan")})
        with self.assertRaises(ValueError):
            store.reserve_inbox("a", "event:1", {**template, "api_hash": "credential"})
        with self.assertRaises(ValueError):
            store.pending_inbox("a", limit=0)

    def test_database_cannot_be_created_in_git_tree_or_relative_cwd(self):
        with self.assertRaises(ValueError):
            DurableStore("data.sqlite3")
        project_tree = Path(store_module.__file__).resolve().parent.parent
        with self.assertRaises(ValueError):
            DurableStore(project_tree / "secret-must-not-exist.sqlite3")
        self.assertFalse((project_tree / "secret-must-not-exist.sqlite3").exists())

    def test_enqueue_idempotent_random_id_and_reopen(self):
        store = self.store()
        job = store.enqueue("a", "intent-1", peer_id=-100123, topic_id=17,
                            reply_id=18, payload_ref="blob:opaque123")
        self.assertEqual(job, store.enqueue("a", "intent-1", peer_id=-100123,
                                           topic_id=17, reply_id=18, payload_ref="blob:opaque123"))
        self.assertTrue(-(2**63) <= job.random_id < 2**63)
        self.assertNotEqual(0, job.random_id)
        with self.assertRaises(ValueError):
            store.enqueue("a", "intent-1", peer_id=-100123,
                          payload_ref="blob:other")
        store.close()
        reopened = self.store()
        self.assertEqual(job.random_id, reopened.get("a", "intent-1").random_id)
        self.assertEqual("queued", reopened.get("a", "intent-1").status)
        self.assertIsNone(reopened.get("b", "intent-1"))
        self.assertNotEqual(job.account_id, reopened.enqueue("b", "intent-1", peer_id=-100123,
                           payload_ref="blob:opaque123").account_id)

    def test_unattempted_lease_expires_and_old_token_fenced(self):
        store = self.store()
        store.enqueue("a", "one", peer_id=-10, payload_ref="p:1")
        first = store.claim("a", lease_seconds=5)
        self.assertEqual("leased", first.status)
        self.assertIsNone(store.claim("a"))
        self.assertIsNone(store.claim("b"))
        self.now[0] += 6
        second = store.claim("a")
        self.assertEqual(first.random_id, second.random_id)
        self.assertNotEqual(first.lease_token, second.lease_token)
        with self.assertRaises(ValueError):
            store.mark_attempted("a", "one", first.lease_token)
        self.assertEqual("attempted", store.mark_attempted("a", "one", second.lease_token).status)
        self.assertIsNone(store.claim("a"))
        self.assertEqual("sent", store.settle("a", "one", second.lease_token,
                                               success=True, remote_message_id=77).status)
        self.assertEqual(77, store.get("a", "one").remote_message_id)
        self.assertIsNone(store.claim("a"))

    def test_attempted_reopen_becomes_unknown_and_never_auto_retries(self):
        store = self.store()
        original = store.enqueue("a", "send-1", peer_id=-101, payload_ref="payload:1")
        lease = store.claim("a")
        store.mark_attempted("a", "send-1", lease.lease_token)
        # Plugin handles may open during an active send without misclassifying it.
        observer = self.store()
        self.assertEqual("attempted", observer.get("a", "send-1").status)
        observer.close()
        store.close()  # crash after send request but before acknowledgement
        reopened = self.store(recover_attempted=True)
        unknown = reopened.get("a", "send-1")
        self.assertEqual("unknown", unknown.status)
        self.assertEqual(original.random_id, unknown.random_id)
        self.assertEqual([unknown], reopened.list_unknown("a"))
        self.assertEqual([], reopened.list_unknown("b"))
        self.assertIsNone(reopened.claim("a"))
        with self.assertRaises(ValueError):
            reopened.mark_attempted("a", "send-1", lease.lease_token)
        # A late outcome with the original fenced token is still recordable.
        self.assertEqual("sent", reopened.settle("a", "send-1", lease.lease_token,
                                               success=True, remote_message_id=56).status)
        self.assertEqual([], reopened.list_unknown("a"))

    def test_failed_send_is_terminal_and_error_is_code_only(self):
        store = self.store()
        store.enqueue("a", "two", peer_id=5, payload_ref="payload:2")
        token = store.claim("a").lease_token
        with self.assertRaises(ValueError):
            store.settle("a", "two", token, success=False, error_code="raw exception / token=abc")
        with self.assertRaises(ValueError):
            store.settle("a", "two", token, success=False, error_code="TIMEOUT")  # not attempted
        store.mark_attempted("a", "two", token)
        self.assertEqual("failed", store.settle("a", "two", token,
                                                  success=False, error_code="TIMEOUT").status)
        self.assertIsNone(store.claim("a"))
        store.close()
        self.assertEqual("failed", self.store().get("a", "two").status)

    def test_account_claim_separation_and_sqlite_wal(self):
        store = self.store()
        store.enqueue("a", "same", peer_id=10, payload_ref="a:1")
        store.enqueue("b", "same", peer_id=10, payload_ref="b:1")
        self.assertEqual("wal", store.db.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual("a", store.claim("a").account_id)
        self.assertEqual("b", store.claim("b").account_id)
        with self.assertRaises(ValueError):
            store.mark_attempted("a", "same", store.get("b", "same").lease_token)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
