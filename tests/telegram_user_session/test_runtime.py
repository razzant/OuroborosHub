"""No Telegram account is used. These tests exercise the *joined* custody seams."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from userbot.presence import PresenceResult
from userbot.runtime import UserSessionBridge, _atomic_json, _read_json
from userbot.action_store import ActionStore
from userbot.actions import ActionAmbiguous, ActionFloodWait, ActionRejected
from userbot.config import Config as SessionConfig, Binding
from userbot.store import DurableStore
from userbot.telegram import AmbiguousSend, SendResult


@dataclass(frozen=True)
class Config:
    account_id: int = 42
    binding_id: str = "b" * 32
    allowed_peer_ids: tuple[int, ...] = (-100123, -100456)

class Client:
    async def get_entity(self, peer):
        return SimpleNamespace(forum=True, monoforum=False)

    async def get_messages(self, peer, *, ids):
        return SimpleNamespace(id=ids, sender_id=42, reply_to=SimpleNamespace(
            reply_to_top_id=80 if ids == 12 else 40,
            reply_to_msg_id=80 if ids == 12 else 40,
            forum_topic=True), action=None)


class Adapter:
    def __init__(self):
        self.client = Client()
        self.calls = []
        self.fail = False

    async def topic_ids(self, peer):
        return {1, 40, 80}

    async def get_me(self):
        return 42

    async def send_text(self, peer, text, *, random_id, topic_id, reply_id):
        self.calls.append((peer, text, random_id, topic_id, reply_id))
        if self.fail:
            raise AmbiguousSend("unknown")
        return SendResult(random_id, 998)


class Host:
    def __init__(self, binding_id="b" * 32):
        self.binding_id = binding_id
        self.turns = []
        self.reports = []
        self.result = PresenceResult("completed", outcome="message", text="reply",
                                     turn_ref="turn-123", delivery_reporting_version=1)
        self.results_by_peer = {}

    def turn(self, event, *, delivery_reporting_version):
        self.turns.append((event, delivery_reporting_version))
        return self.results_by_peer.get(int(event["conversation_id"]), self.result)

    def work(self, ref):
        return self.result

    def report_delivered(self, **kwargs):
        self.reports.append(kwargs)
        return PresenceResult("completed")


class FakeActions:
    """Separates read-only preflight from the one physical mutation boundary."""

    def __init__(self):
        self.calls = []
        self.preflights = []
        self.failure = None
        self.preflight_failure = None

    async def _own_target(self, peer, message, topic):
        self.preflights.append(("own", peer, message, topic))
        if self.preflight_failure:
            raise self.preflight_failure
        return object(), SimpleNamespace(media=None, entities=None)

    async def _target(self, peer, message, topic):
        self.preflights.append(("target", peer, message, topic))
        if self.preflight_failure:
            raise self.preflight_failure
        return object(), SimpleNamespace()

    async def edit(self, peer, message, text, *, topic_id):
        self.calls.append(("edit", peer, message, text, topic_id))
        if self.failure:
            raise self.failure

    async def delete(self, peer, message, *, topic_id):
        self.calls.append(("delete", peer, message, topic_id))
        if self.failure:
            raise self.failure

    async def react(self, peer, message, emoji, *, topic_id):
        self.calls.append(("react", peer, message, emoji, topic_id))
        if self.failure:
            raise self.failure


def event(*, message_id=11, text="hi", topic=40, edit=False, chat_id=-100123):
    header = SimpleNamespace(reply_to_top_id=topic, reply_to_msg_id=9,
                             forum_topic=True)
    msg = SimpleNamespace(id=message_id, out=False, mentioned=True, reply_to=header,
                          action=None, edit_date=datetime(2026, 10, 12, tzinfo=timezone.utc))
    return SimpleNamespace(message=msg, chat_id=chat_id, sender_id=77,
                           raw_text=text)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DurableStore(self.root / "store.sqlite")
        self.addCleanup(self.store.close)
        self.host = Host()
        self.adapter = Adapter()
        self.bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, self.host)

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_atomic_private_write_rejects_symlinked_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        redirected = self.root / "incoming"
        try:
            os.symlink(outside, redirected, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink not available on this host")
        with self.assertRaisesRegex(ValueError, "redirected"):
            _atomic_json(redirected / "event.json", {"private": "content"})
        self.assertFalse((outside / "event.json").exists())

    def test_ingress_backpressure_stops_capture_with_visible_gap(self):
        from userbot import store as store_module
        original = store_module.RAW_INGRESS_MAX_EVENTS
        store_module.RAW_INGRESS_MAX_EVENTS = 2
        try:
            self.bridge.spool_update(event(message_id=1))
            self.bridge.spool_update(event(message_id=2))
            with self.assertRaises(store_module.IngressBackpressure):
                self.bridge.spool_update(event(message_id=3))
            self.assertEqual(self.store.pending_raw_count("42"), 2)
            self.assertEqual(len(list((self.root / "incoming").glob("*.json"))), 2)
        finally:
            store_module.RAW_INGRESS_MAX_EVENTS = original

    def test_oversized_allowed_update_is_a_visible_uncaptured_gap(self):
        from userbot.runtime import IngressTooLarge
        with self.assertRaises(IngressTooLarge):
            self.bridge.spool_update(event(text="x" * 60001))
        self.assertEqual(self.store.pending_raw_count("42"), 0)

    def test_persistent_host_failure_exposes_repair_needed_and_paces_retry(self):
        self.host.result = PresenceResult("retry", code="host_down")
        path = self.bridge.spool_update(event())
        for _ in range(5):
            self.assertEqual(self.run_async(self.bridge.process_incoming()), 0)
        self.assertTrue(path.exists())
        health = _read_json(self.root / "inbound-health.json")
        row = health["peers"]["-100123"]
        self.assertTrue(row["repair_needed"])
        self.assertEqual(row["code"], "HOST_UNAVAILABLE")
        self.assertEqual(len(self.host.turns), 5)
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 0)
        self.assertEqual(len(self.host.turns), 5)  # not every three seconds forever

    def test_peer_success_never_erases_another_blocked_peer(self):
        # Review defect 1: peer A blocked at attempts>=5 while peer B succeeds;
        # the old single-slot health file let B's success overwrite A's warning.
        self.host.results_by_peer = {-100123: PresenceResult("retry", code="host_down")}
        path_a = self.bridge.spool_update(event(message_id=11))
        for _ in range(5):
            self.run_async(self.bridge.process_incoming())
        health = _read_json(self.root / "inbound-health.json")
        self.assertTrue(health["peers"]["-100123"]["repair_needed"])
        # Peer B (allowlisted in Config) succeeds and ACKs.
        path_b = self.bridge.spool_update(event(chat_id=-100456, message_id=12))
        self.run_async(self.bridge.process_incoming())
        self.assertFalse(path_b.exists())
        health = _read_json(self.root / "inbound-health.json")
        row = health["peers"]["-100123"]
        self.assertTrue(row["repair_needed"])
        self.assertEqual(row["attempts"], 5)
        self.assertNotIn("-100456", health["peers"])
        # Restart rebuilds blocked state from the same projection.
        revived = UserSessionBridge(self.root, Config(), self.adapter, self.store, self.host)
        self.assertEqual(revived._ingress_failures, {-100123: ("HOST_UNAVAILABLE", 5)})

    def test_raw_event_precedes_host_and_outgoing_intent_survives_crash(self):
        path = self.bridge.spool_update(event())
        self.assertTrue(path.exists())
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 1)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.host.turns), 1)
        payload, version = self.host.turns[0]
        self.assertEqual((payload["conversation_id"], payload["thread_id"], version),
                         ("-100123", "40", 1))
        intent = "auto_" + __import__("hashlib").sha256(payload["source_event_id"].encode()).hexdigest()
        job = self.store.get("42", intent)
        self.assertEqual(job.status, "queued")
        self.assertEqual(_read_json(self.root / "outgoing" / (job.payload_ref + ".json"))["text"], "reply")
        # Simulated restart: same raw event has a stable key and cannot mint a
        # second model turn/send even when Telegram redelivers it.
        self.bridge.spool_update(event())
        self.run_async(self.bridge.process_incoming())
        # A locally ACKed event is not even re-posted after catch-up.
        self.assertEqual(len(self.host.turns), 1)
        self.assertEqual(self.store.get("42", intent).random_id, job.random_id)
        self.run_async(self.bridge.send_one())
        self.assertEqual(self.store.get("42", intent).status, "sent")
        self.assertEqual(self.run_async(self.bridge.report_sent()), 1)
        self.assertEqual(self.run_async(self.bridge.report_sent()), 0)
        self.assertEqual(len(self.host.reports), 1)
        self.assertEqual(self.host.reports[0]["task_id"], "turn-123")

    def test_ambiguous_send_remains_unknown_after_startup_and_never_retried(self):
        self.bridge.spool_update(event())
        self.run_async(self.bridge.process_incoming())
        self.adapter.fail = True
        self.run_async(self.bridge.send_one())
        self.assertEqual(len(self.adapter.calls), 1)
        with DurableStore(self.root / "store.sqlite", recover_attempted=True) as recovered:
            self.assertEqual(len(recovered.list_unknown("42")), 1)
            bridge = UserSessionBridge(self.root, Config(), self.adapter, recovered, self.host)
            self.assertFalse(self.run_async(bridge.send_one()))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_slow_preflight_losing_lease_does_not_crash_or_send(self):
        # Review defect (t_glm_1, send_one): a catalog/reply preflight that
        # outlasts the 60s claim lease must neither crash the companion nor
        # treat the intent as sent; it stays re-claimable with the same
        # intent_id and frozen random_id.
        self.bridge.spool_update(event())
        self.run_async(self.bridge.process_incoming())
        intent_row = self.store.db.execute(
            "SELECT intent_id, random_id FROM outgoing").fetchone()
        slow_steps = {"n": 0}
        real_sleep = asyncio.sleep
        async def slow_catalog(_peer):
            # Consume more than the whole 60s lease inside the preflight.
            if slow_steps["n"] == 0:
                slow_steps["n"] += 1
                await real_sleep(0)
                self.store.db.execute(
                    "UPDATE outgoing SET lease_until=? WHERE intent_id=?",
                    (self.store.clock() - 1, intent_row["intent_id"]))
            return {1, 40, 80}
        self.adapter.topic_ids = slow_catalog
        self.assertFalse(self.run_async(self.bridge.send_one()))
        self.assertEqual(self.adapter.calls, [])
        after = self.store.db.execute(
            "SELECT status, random_id FROM outgoing").fetchone()
        # Leased (not attempted/unknown/sent): the expired lease is re-claimable.
        self.assertEqual(after["status"], "leased")
        self.assertEqual(after["random_id"], intent_row["random_id"])
        # After the expired lease passes, a normal claim sends exactly once.
        self.adapter.topic_ids = Adapter().topic_ids
        self.assertTrue(self.run_async(self.bridge.send_one()))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.store.get("42", intent_row["intent_id"]).status, "sent")

    def test_acknowledged_tl_without_matching_message_id_stays_unresolved(self):
        self.bridge.spool_update(event())
        self.run_async(self.bridge.process_incoming())
        async def no_matching_id(peer, text, *, random_id, topic_id, reply_id):
            self.adapter.calls.append((peer, text, random_id, topic_id, reply_id))
            return SendResult(random_id, None)
        self.adapter.send_text = no_matching_id
        self.assertTrue(self.run_async(self.bridge.send_one()))
        row = self.store.db.execute("SELECT status, remote_message_id FROM outgoing").fetchone()
        self.assertEqual(tuple(row), ("attempted", None))
        self.assertEqual(self.run_async(self.bridge.report_sent()), 0)
        self.assertEqual(self.host.reports, [])
        with DurableStore(self.root / "store.sqlite", recover_attempted=True) as recovered:
            bridge = UserSessionBridge(self.root, Config(), self.adapter, recovered, self.host)
            self.assertEqual(len(recovered.list_unknown("42")), 1)
            self.assertFalse(self.run_async(bridge.send_one()))
            self.assertEqual(self.run_async(bridge.report_sent()), 0)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_failed_host_receipt_is_paced_and_does_not_starve_another(self):
        clock = [1000.0]
        self.store.clock = lambda: clock[0]
        self.bridge.spool_update(event(message_id=11))
        self.bridge.spool_update(event(message_id=12, topic=80))
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 2)
        self.run_async(self.bridge.send_one())
        self.run_async(self.bridge.send_one())
        responses = iter([PresenceResult("retry", code="host_down"),
                          PresenceResult("completed"), PresenceResult("completed")])
        self.host.report_delivered = lambda **kwargs: next(responses)
        self.assertEqual(self.run_async(self.bridge.report_sent(limit=1)), 0)
        self.assertEqual(self.store.report_counts("42"),
                         {"pending": 2, "retrying": 1, "invalid": 0, "unsupported": 0})
        self.assertEqual(self.run_async(self.bridge.report_sent(limit=1)), 1)
        self.assertEqual(self.run_async(self.bridge.report_sent(limit=1)), 0)
        clock[0] += 61
        self.assertEqual(self.run_async(self.bridge.report_sent(limit=1)), 1)
        self.assertEqual(self.store.report_counts("42"),
                         {"pending": 0, "retrying": 0, "invalid": 0, "unsupported": 0})

    def test_legacy_host_send_is_not_misreported_as_invalid_or_delivered(self):
        self.host.result = PresenceResult("completed", outcome="message", text="legacy",
                                          delivery_reporting_version=0)
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, self.host,
                                   delivery_reporting_version=0)
        bridge.spool_update(event())
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        self.run_async(bridge.send_one())
        self.assertEqual(self.run_async(bridge.report_sent()), 0)
        self.assertEqual(self.host.reports, [])
        self.assertEqual(self.store.report_counts("42")["unsupported"], 1)

    def test_corrupt_report_file_is_quarantined_without_blocking_valid_receipt(self):
        self.bridge.spool_update(event())
        self.run_async(self.bridge.process_incoming())
        self.run_async(self.bridge.send_one())
        for ref in ("000-corrupt", "001-corrupt"):
            self.store.enqueue("42", "auto_" + ref, peer_id=-100123, payload_ref=ref)
            job = self.store.claim("42")
            self.store.mark_attempted("42", job.intent_id, job.lease_token)
            self.store.settle("42", job.intent_id, job.lease_token,
                              success=True, remote_message_id=999)
        (self.root / "outgoing" / "000-corrupt.json").write_text("{malformed", encoding="utf-8")
        bad = {"source_event_id": "bad", "delivery_reporting_version": 1,
               "peer_id": -100123, "binding_id": "b" * 32, "text": "not delivered"}
        (self.root / "outgoing" / "001-corrupt.json").write_text(
            json.dumps(bad), encoding="utf-8")
        self.assertEqual(self.run_async(self.bridge.report_sent()), 1)
        self.assertEqual(len(self.host.reports), 1)
        self.assertEqual(self.host.reports[0]["provider_message_id"], 998)
        original = [p for p in (self.root / "outgoing").glob("*.invalid-*")
                    if not p.name.endswith(".diagnostic")]
        self.assertEqual(len(original), 2)
        self.assertEqual({p.read_text(encoding="utf-8") for p in original},
                         {"{malformed", json.dumps(bad)})
        self.assertEqual([self.store.get("42", "auto_" + ref).reported
                          for ref in ("000-corrupt", "001-corrupt")], [2, 2])
        for path in original:
            diagnostic = path.with_name(path.name + ".diagnostic")
            self.assertEqual(_read_json(diagnostic)["error_code"],
                             "MALFORMED_DELIVERY_PAYLOAD")
        self.assertEqual(self.run_async(self.bridge.report_sent()), 0)

    def test_raw_event_is_not_rebound_to_new_presence_binding(self):
        path = self.bridge.spool_update(event())
        changed = UserSessionBridge(self.root, Config(binding_id="c" * 32),
                                    self.adapter, self.store, self.host)
        self.assertEqual(self.run_async(changed.process_incoming()), 0)
        self.assertTrue(path.with_suffix(".held").exists())
        self.assertEqual(self.host.turns, [])

    def test_duplicate_after_binding_edit_preserves_original_enrollment(self):
        original = self.bridge.spool_update(event())
        captured = _read_json(original)
        changed = UserSessionBridge(self.root, Config(binding_id="c" * 32),
                                    self.adapter, self.store, Host("c" * 32))
        self.assertEqual(changed.spool_update(event()), original)
        self.assertEqual(_read_json(original), captured)
        self.assertEqual(self.run_async(changed.process_incoming()), 0)
        self.assertTrue(original.with_suffix(".held").exists())
        self.assertIsNone(changed.spool_update(event()))
        # An ACKed event's bodyless tombstone also survives changed enrollment.
        self.bridge.spool_update(event(message_id=19))
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 1)
        self.assertIsNone(changed.spool_update(event(message_id=19)))

    def test_edits_have_distinct_identity_and_self_message_is_ignored(self):
        self.bridge.spool_update(event())
        edited = self.bridge.spool_update(event(edit=True), kind="edit")
        self.assertTrue(edited.exists())
        self.assertEqual(len(list((self.root / "incoming").glob("*.json"))), 2)
        own = event()
        own.message.out = True
        self.assertIsNone(self.bridge.spool_update(own))

    def test_two_different_edits_in_one_telegram_second_have_distinct_host_ids(self):
        first = self.bridge.spool_update(event(edit=True, text="first"), kind="edit")
        second = self.bridge.spool_update(event(edit=True, text="second"), kind="edit")
        self.assertNotEqual(first, second)
        self.assertEqual(self.bridge.spool_update(event(edit=True, text="first"), kind="edit"), first)
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 2)
        ids = [item[0]["source_event_id"] for item in self.host.turns]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(":edit:" in item for item in ids))

    def test_incomplete_forum_catalog_does_not_attempt_text_or_exhaust_loop(self):
        _atomic_json(self.root / "outgoing" / "good.json",
                     {"text": "hello", "peer_id": -100123, "topic_id": 40,
                      "reply_id": None, "binding_id": "b" * 32})
        self.store.enqueue("42", "good", peer_id=-100123, payload_ref="good", topic_id=40)
        async def incomplete(_peer):
            raise RuntimeError("incomplete forum catalog")
        self.adapter.topic_ids = incomplete
        self.assertFalse(self.run_async(self.bridge.send_one()))
        self.assertEqual(self.store.get("42", "good").status, "leased")
        self.assertEqual(self.adapter.calls, [])

    def test_incomplete_catalog_on_refresh_does_not_mark_attempted(self):
        _atomic_json(self.root / "outgoing" / "good.json",
                     {"text": "hello", "peer_id": -100123, "topic_id": 40,
                      "reply_id": None, "binding_id": "b" * 32})
        self.store.enqueue("42", "good", peer_id=-100123, payload_ref="good", topic_id=40)
        calls = 0
        async def changing(_peer):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {1}  # The initial catalog lacks the requested topic.
            raise RuntimeError("forum pagination stopped")
        self.adapter.topic_ids = changing
        self.assertFalse(self.run_async(self.bridge.send_one()))
        self.assertEqual(calls, 2)
        self.assertEqual(self.store.get("42", "good").status, "leased")
        self.assertEqual(self.adapter.calls, [])

    def test_host_unavailable_leaves_pending_and_no_telegram_send(self):
        self.host.result = PresenceResult("retry", code="transport_unknown")
        path = self.bridge.spool_update(event())
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(len(self.store.pending_inbox("42")), 1)

    def test_pending_and_refused_work_refs_survive_restart_and_do_not_lose_reply(self):
        class DeferredHost(Host):
            def __init__(self):
                super().__init__()
                self.result = PresenceResult("retry", work_ref="child-1")
                self.work_refs = []

            def work(self, ref):
                self.work_refs.append(ref)
                return PresenceResult("completed", outcome="message", text="later",
                                      turn_ref="child-1", delivery_reporting_version=1)

        host = DeferredHost()
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        path = bridge.spool_update(event())
        self.assertEqual(self.run_async(bridge.process_incoming()), 0)
        self.assertEqual(_read_json(path)["work_ref"], "child-1")
        # A new process resumes by exact child handle, not a new Host turn.
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        self.assertEqual(host.work_refs, ["child-1"])
        self.assertEqual(len(host.turns), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.db.execute("SELECT status FROM outgoing").fetchone()[0], "queued")

    def test_blocked_parent_with_admitted_child_is_polled_not_held(self):
        class RefusedParent(Host):
            def work(self, ref):
                return PresenceResult("completed", outcome="message", text="child reply",
                                      work_ref=ref, delivery_reporting_version=1)

        host = RefusedParent()
        host.result = PresenceResult("blocked", work_ref="child-3", code="presence_resources_unavailable")
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        path = bridge.spool_update(event())
        self.assertEqual(self.run_async(bridge.process_incoming()), 0)
        self.assertTrue(path.exists())
        self.assertEqual(_read_json(path)["work_ref"], "child-3")
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        self.assertEqual(len(host.turns), 1)
        self.assertFalse(path.exists())

    def test_deferred_work_is_polled_across_restart(self):
        class Deferred(Host):
            def work(self, ref):
                return PresenceResult("completed", outcome="message", text="late",
                                      work_ref=ref, delivery_reporting_version=1)

        host = Deferred()
        host.result = PresenceResult("completed", outcome="deferred", work_ref="child-4")
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        path = bridge.spool_update(event())
        self.assertEqual(self.run_async(bridge.process_incoming()), 0)
        self.assertEqual(_read_json(path)["work_ref"], "child-4")
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.db.execute("SELECT status FROM outgoing").fetchone()[0], "queued")

    def test_deferred_interim_and_later_child_reply_have_distinct_custody(self):
        class Deferred(Host):
            def work(self, ref):
                return PresenceResult("completed", outcome="message", text="finished",
                                      work_ref=ref, delivery_reporting_version=1)

        host = Deferred()
        host.result = PresenceResult("completed", outcome="deferred", text="working",
                                     work_ref="child-5", turn_ref="parent-5",
                                     delivery_reporting_version=1)
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        bridge.spool_update(event())
        self.assertEqual(self.run_async(bridge.process_incoming()), 0)
        jobs = self.store.db.execute("SELECT intent_id FROM outgoing").fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0][0].endswith("_interim"))
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        jobs = self.store.db.execute("SELECT intent_id FROM outgoing ORDER BY rowid").fetchall()
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0][0], jobs[1][0])
        self.assertTrue(self.run_async(bridge.send_one()))
        self.assertTrue(self.run_async(bridge.send_one()))
        self.assertEqual(self.run_async(bridge.report_sent()), 2)
        self.assertEqual([item["part_id"] for item in host.reports], ["0", "1"])
        self.assertEqual(host.reports[0]["delivery_id"], host.reports[1]["delivery_id"])
        self.assertIsNone(bridge.spool_update(event()))
        self.assertEqual(self.run_async(bridge.report_sent()), 0)

    def test_admitted_work_survives_deleted_reply_target(self):
        class PendingHost(Host):
            def work(self, ref):
                return PresenceResult("completed", outcome="silent", work_ref=ref)

        host = PendingHost()
        host.result = PresenceResult("pending", work_ref="child-2")
        bridge = UserSessionBridge(self.root, Config(), self.adapter, self.store, host)
        inbound = event()
        inbound.message.mentioned = False
        path = bridge.spool_update(inbound)
        self.assertEqual(self.run_async(bridge.process_incoming()), 0)
        async def deleted(_peer, *, ids):
            return None
        self.adapter.client.get_messages = deleted
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        self.assertFalse(path.exists())
        self.assertEqual(len(host.turns), 1)

    def test_transient_pre_send_telegram_lookup_does_not_kill_bridge(self):
        inbound = event()
        inbound.message.mentioned = False
        path = self.bridge.spool_update(inbound)
        async def catalog_offline(_peer):
            raise ConnectionError("temporary")
        self.adapter.topic_ids = catalog_offline
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.host.turns, [])
        self.adapter.topic_ids = Adapter().topic_ids
        async def offline(_peer, *, ids):
            raise ConnectionError("temporary")
        self.adapter.client.get_messages = offline
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 1)
        self.assertFalse(self.run_async(self.bridge.send_one()))
        self.assertEqual(self.store.db.execute("SELECT status FROM outgoing").fetchone()[0], "leased")
        self.assertEqual(self.adapter.calls, [])

    def test_capture_fifo_and_blocked_peer_cannot_be_overtaken(self):
        # IDs are chosen so hash-file order is deliberately reverse of ingress.
        from userbot.runtime import _source_filename
        pair = next((a, b) for a in range(1, 50) for b in range(a + 1, 50)
                    if _source_filename(42, -100123, a, "new", "") >
                       _source_filename(42, -100123, b, "new", ""))
        for mid in pair:
            self.bridge.spool_update(event(message_id=mid))
        self.run_async(self.bridge.process_incoming(limit=1))
        self.run_async(self.bridge.process_incoming(limit=1))
        self.assertEqual([int(row[0]["message"]["message_id"]) for row in self.host.turns], list(pair))
        self.host.result = PresenceResult("pending", work_ref="waiting")
        self.bridge.spool_update(event(message_id=100))
        self.bridge.spool_update(event(message_id=101))
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 0)
        self.assertEqual([int(row[0]["message"]["message_id"]) for row in self.host.turns],
                         [*pair, 100])

    def test_blocked_peer_does_not_starve_another_peer(self):
        config = SessionConfig(123, "a" * 32, "+15551234567",
                               allowed_peer_ids=(-100123, 200), account_id=42,
                               bindings=(Binding(-100123, 40, "b" * 32),
                                         Binding(200, None, "c" * 32)))
        other = Host("c" * 32)
        other.result = PresenceResult("completed", outcome="silent")
        self.host.result = PresenceResult("pending", work_ref="slow")
        bridge = UserSessionBridge(self.root, config, self.adapter, self.store, self.host,
                                   presence_factory=lambda _: other)
        bridge.spool_update(event(message_id=11))
        bridge.spool_update(event(message_id=12))
        different = event(message_id=13)
        different.chat_id = 200
        bridge.spool_update(different)
        async def topics(peer):
            return None if peer == 200 else {1, 40, 80}
        self.adapter.topic_ids = topics
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        self.assertEqual([int(row[0]["message"]["message_id"]) for row in self.host.turns], [11])
        self.assertEqual([int(row[0]["message"]["message_id"]) for row in other.turns], [13])

    def test_acked_raw_scrubbed_and_duplicate_retains_same_sequence(self):
        path = self.bridge.spool_update(event())
        row = self.store.db.execute("SELECT seq,initial_json FROM raw_ingress").fetchone()
        self.assertIn("hi", row["initial_json"])
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 1)
        after = self.store.db.execute("SELECT seq,initial_json,initial_digest FROM raw_ingress").fetchone()
        self.assertEqual(after["seq"], row["seq"])
        self.assertEqual(after["initial_json"], "")
        self.assertEqual(len(after["initial_digest"]), 64)
        self.assertFalse(path.exists())
        self.assertIsNone(self.bridge.spool_update(event()))

    def test_overlong_automatic_reply_is_held_before_ack_or_send(self):
        self.host.result = PresenceResult("completed", outcome="message", text="x" * 5000,
                                          delivery_reporting_version=1)
        path = self.bridge.spool_update(event())
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 0)
        self.assertFalse(path.exists())
        self.assertEqual(_read_json(path.with_suffix(".held"))["failure_code"], "REPLY_TOO_LONG")
        self.assertEqual(self.store.pending_raw_count("42"), 0)
        self.assertEqual(self.store.db.execute("SELECT status FROM inbox_delivery").fetchone()[0], "pending")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outgoing").fetchone()[0], 0)
        self.assertEqual(self.adapter.calls, [])

    def test_new_forum_root_refreshes_before_selecting_binding(self):
        config = SessionConfig(123, "a" * 32, "+15551234567",
                               allowed_peer_ids=(-100123,), account_id=42,
                               bindings=(Binding(-100123, None, "b" * 32),
                                         Binding(-100123, 90, "c" * 32)))
        topic_host = Host("c" * 32)
        topic_host.result = PresenceResult("completed", outcome="silent")
        bridge = UserSessionBridge(self.root, config, self.adapter, self.store, self.host,
                                   presence_factory=lambda _: topic_host)
        calls = 0
        async def catalog(_peer):
            nonlocal calls
            calls += 1
            return {1, 40} if calls == 1 else {1, 40, 90}
        self.adapter.topic_ids = catalog
        incoming = event()
        incoming.message.reply_to.reply_to_top_id = None
        incoming.message.reply_to.reply_to_msg_id = 90
        bridge.spool_update(incoming)
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        self.assertEqual(calls, 2)
        self.assertEqual(self.host.turns, [])
        self.assertEqual(topic_host.turns[0][0]["thread_id"], "90")

    def test_unknown_forum_root_does_not_borrow_general_binding(self):
        config = SessionConfig(123, "a" * 32, "+15551234567",
                               allowed_peer_ids=(-100123,), account_id=42,
                               bindings=(Binding(-100123, None, "b" * 32),))
        bridge = UserSessionBridge(self.root, config, self.adapter, self.store, self.host)
        async def catalog(_peer):
            return {1, 40}
        self.adapter.topic_ids = catalog
        async def unknown_root(_peer, *, ids):
            return SimpleNamespace(id=ids, action=SimpleNamespace(CONSTRUCTOR_ID=0x0D999256),
                                   reply_to=None)
        self.adapter.client.get_messages = unknown_root
        incoming = event()
        incoming.message.reply_to.reply_to_top_id = None
        incoming.message.reply_to.reply_to_msg_id = 90
        path = bridge.spool_update(incoming)
        self.assertEqual(self.run_async(bridge.process_incoming()), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.host.turns, [])

    def test_ordinary_general_reply_uses_fetched_target_proof(self):
        async def ordinary(_peer, *, ids):
            return SimpleNamespace(id=ids, action=None, reply_to=None)
        self.adapter.client.get_messages = ordinary
        incoming = event()
        incoming.message.reply_to.reply_to_top_id = None
        incoming.message.reply_to.reply_to_msg_id = 92
        self.bridge.spool_update(incoming)
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 1)
        self.assertEqual(self.host.turns[0][0]["thread_id"], "")

    def test_general_reply_chain_is_resolved_for_inbound_and_outbound(self):
        async def chain(_peer, *, ids):
            if ids == 201:
                return SimpleNamespace(id=201, action=None, reply_to=None)
            if ids in (200, 202):
                return SimpleNamespace(id=ids, action=None, reply_to=SimpleNamespace(
                    reply_to_top_id=None, reply_to_msg_id=201 if ids == 200 else 200,
                    forum_topic=True))
            return None
        self.adapter.client.get_messages = chain
        incoming = event(message_id=202)
        incoming.message.reply_to.reply_to_top_id = None
        incoming.message.reply_to.reply_to_msg_id = 200
        self.bridge.spool_update(incoming)
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 1)
        self.assertEqual(self.host.turns[0][0]["thread_id"], "")
        self.assertTrue(self.run_async(self.bridge.send_one()))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_cyclic_general_ancestry_stays_pending_without_host_turn(self):
        async def cycle(_peer, *, ids):
            return SimpleNamespace(id=ids, action=None, reply_to=SimpleNamespace(
                reply_to_top_id=None, reply_to_msg_id=201 if ids == 200 else 200,
                forum_topic=True))
        self.adapter.client.get_messages = cycle
        incoming = event(message_id=202)
        incoming.message.reply_to.reply_to_top_id = None
        incoming.message.reply_to.reply_to_msg_id = 200
        path = self.bridge.spool_update(incoming)
        self.assertEqual(self.run_async(self.bridge.process_incoming()), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.host.turns, [])

    def test_unknown_explicit_top_never_borrows_reply_targets_topic(self):
        config = SessionConfig(123, "a" * 32, "+15551234567",
                               allowed_peer_ids=(-100123,), account_id=42,
                               bindings=(Binding(-100123, 40, "b" * 32),
                                         Binding(-100123, 80, "c" * 32)))
        other = Host("c" * 32)
        bridge = UserSessionBridge(self.root, config, self.adapter, self.store,
                                   self.host, presence_factory=lambda _key: other)
        incoming = event(message_id=202)
        incoming.message.reply_to.reply_to_top_id = 999  # explicit unknown topic
        incoming.message.reply_to.reply_to_msg_id = 12  # fetched target is in topic 80
        path = bridge.spool_update(incoming)
        self.assertEqual(self.run_async(bridge.process_incoming()), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.host.turns, [])
        self.assertEqual(other.turns, [])

    def test_inbound_attempt_budget_counts_failed_peers(self):
        config = Config(allowed_peer_ids=tuple(range(1, 15)))
        self.host.result = PresenceResult("retry")
        bridge = UserSessionBridge(self.root, config, self.adapter, self.store, self.host)
        for peer in config.allowed_peer_ids:
            update = event(message_id=11)
            update.chat_id = peer
            bridge.spool_update(update)
        self.assertEqual(self.run_async(bridge.process_incoming(limit=5)), 0)
        self.assertEqual(len(self.host.turns), 5)
        self.assertEqual(self.run_async(bridge.process_incoming(limit=5)), 0)
        self.assertEqual(len(self.host.turns), 10)

    def test_invalid_first_intent_retires_without_starving_later_send(self):
        from userbot.runtime import _atomic_json
        _atomic_json(self.root / "outgoing" / "bad.json",
                     {"text": "wrong topic", "peer_id": -100123,
                      "topic_id": 999, "reply_id": None, "binding_id": "b" * 32})
        _atomic_json(self.root / "outgoing" / "good.json",
                     {"text": "second", "peer_id": -100123,
                      "topic_id": 40, "reply_id": None, "binding_id": "b" * 32})
        self.store.enqueue("42", "bad", peer_id=-100123, payload_ref="bad", topic_id=999)
        self.store.enqueue("42", "good", peer_id=-100123, payload_ref="good", topic_id=40)
        self.assertTrue(self.run_async(self.bridge.send_one()))
        self.assertEqual(self.store.get("42", "bad").status, "failed")
        self.assertEqual(self.store.get("42", "bad").error_code, "TOPIC_UNAVAILABLE")
        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(self.run_async(self.bridge.send_one()))
        self.assertEqual(self.store.get("42", "good").status, "sent")
        self.assertEqual(len(self.adapter.calls), 1)

    def test_bounded_scan_rotates_past_older_held_events(self):
        from userbot.runtime import _atomic_json
        self.bridge.spool_update(event(message_id=500))
        for index in range(101):
            old = self.bridge.spool_update(event(message_id=index + 1))
            raw = _read_json(old)
            raw["binding_revision"] = "f" * 64  # old enrollment: must not be sent here
            _atomic_json(old, raw)
        # One pass sees a bounded prefix; its cursor makes later records
        # reachable despite the 101 permanently retained older events.
        for _ in range(3):
            self.run_async(self.bridge.process_incoming(limit=100))
        self.assertEqual(len(self.host.turns), 1)
        self.assertEqual(self.host.turns[0][0]["message"]["message_id"], "500")

    def test_two_topics_select_two_exact_owner_bindings_and_keep_reply_scope(self):
        config = SessionConfig(123, "a" * 32, "+15551234567",
                               allowed_peer_ids=(-100123,), account_id=42,
                               bindings=(Binding(-100123, 40, "b" * 32),
                                         Binding(-100123, 80, "c" * 32)))
        second = Host("c" * 32)
        bridge = UserSessionBridge(
            self.root, config, self.adapter, self.store, self.host,
            presence_factory=lambda binding: second if binding == "c" * 32 else self.host)
        first_file = bridge.spool_update(event(message_id=11, topic=40))
        second_file = bridge.spool_update(event(message_id=12, topic=80))
        self.assertEqual(self.run_async(bridge.process_incoming()), 2)
        self.assertFalse(first_file.exists())
        self.assertFalse(second_file.exists())
        self.assertEqual(self.host.turns[0][0]["thread_id"], "40")
        self.assertEqual(second.turns[0][0]["thread_id"], "80")
        self.run_async(bridge.send_one())
        self.run_async(bridge.send_one())
        self.assertEqual([call[3] for call in self.adapter.calls], [40, 80])
        self.assertEqual(self.run_async(bridge.report_sent()), 2)
        self.assertEqual(len(self.host.reports), 1)
        self.assertEqual(len(second.reports), 1)

    def test_mapping_change_cannot_dispatch_prior_topic_intent(self):
        config = SessionConfig(123, "a" * 32, "+15551234567",
                               allowed_peer_ids=(-100123,), account_id=42,
                               bindings=(Binding(-100123, 40, "b" * 32),))
        bridge = UserSessionBridge(self.root, config, self.adapter, self.store, self.host)
        bridge.spool_update(event())
        self.assertEqual(self.run_async(bridge.process_incoming()), 1)
        changed = SessionConfig(123, "a" * 32, "+15551234567",
                                allowed_peer_ids=(-100123,), account_id=42,
                                bindings=(Binding(-100123, 40, "c" * 32),))
        bridge.config = changed
        self.assertTrue(self.run_async(bridge.send_one()))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.db.execute(
            "SELECT status FROM outgoing WHERE account_id='42'").fetchone()[0], "failed")
        self.assertEqual(self.run_async(bridge.report_sent()), 0)


class ActionDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.action_store = ActionStore(self.root / "actions.sqlite")
        self.addCleanup(self.action_store.close)
        self.text_store = DurableStore(self.root / "texts.sqlite")
        self.addCleanup(self.text_store.close)
        self.actions = FakeActions()
        self.adapter = Adapter()
        self.config = SessionConfig(123, "a" * 32, "+15551234567",
                                    allowed_peer_ids=(-100123,), account_id=42,
                                    bindings=(Binding(-100123, 40, "b" * 32),))
        self.bridge = self.make_bridge()

    def make_bridge(self, *, config=None, action_store=None):
        return UserSessionBridge(self.root, config or self.config, self.adapter,
                                 self.text_store, Host(), action_store=action_store or self.action_store,
                                 actions=self.actions)

    def enqueue(self, intent, operation="edit", *, binding=None, topic=40,
                content="a changed message"):
        binding = binding or "b" * 32
        extra = {}
        if operation != "delete":
            payload = {"operation": operation, "peer_id": -100123, "topic_id": topic,
                       "message_id": 71, "binding_id": binding,
                       ("text" if operation == "edit" else "emoji"): content}
            ref = hashlib.sha256(f"42\0{intent}".encode()).hexdigest()
            path = self.root / "actions" / (ref + ".json")
            _atomic_json(path, payload)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            extra = {"payload_ref": ref, "payload_sha256": digest}
        return self.action_store.enqueue("42", intent, operation=operation,
                                         peer_id=-100123, topic_id=topic,
                                         message_id=71, binding_id=binding, **extra)

    def run_once(self, bridge=None):
        return asyncio.run((bridge or self.bridge).process_action_once())

    def test_slow_preflight_losing_action_lease_does_not_crash_or_mutate(self):
        # Mirror of the send_one lease class: an action preflight that outlasts
        # the 60s claim lease must neither crash the companion nor mutate; the
        # leased-but-unattempted intent is re-claimed later with the same id.
        self.enqueue("e")
        row = self.action_store.db.execute(
            "SELECT intent_id FROM actions WHERE intent_id='e'").fetchone()
        self.assertIsNotNone(row)
        slow = {"armed": True}
        real_target = self.actions._own_target
        async def draining(peer, message, topic):
            if slow["armed"]:
                slow["armed"] = False
                self.action_store.db.execute(
                    "UPDATE actions SET lease_until=? WHERE intent_id='e'",
                    (self.action_store.clock() - 1,))
            return await real_target(peer, message, topic)
        self.actions._own_target = draining
        self.assertFalse(self.run_once())
        self.assertEqual(self.actions.calls, [])
        after = self.action_store.get("42", "e")
        self.assertEqual(after.status, "leased")
        # A normal later claim performs exactly one mutation.
        self.actions._own_target = real_target
        self.assertTrue(self.run_once())
        self.assertEqual([c[0] for c in self.actions.calls], ["edit"])
        self.assertEqual(self.action_store.get("42", "e").status, "sent")

    def test_single_edit_delete_react_have_one_mutation_each(self):
        self.enqueue("e")
        self.enqueue("d", "delete")
        self.enqueue("r", "react", content="✅")
        for _ in range(3):
            self.assertTrue(self.run_once())
        self.assertFalse(self.run_once())
        self.assertEqual([c[0] for c in self.actions.calls], ["edit", "delete", "react"])
        self.assertEqual([p[0] for p in self.actions.preflights], ["own", "own", "target"])
        self.assertEqual([self.action_store.get("42", key).status for key in "edr"],
                         ["sent"] * 3)
        self.assertEqual(self.adapter.calls, [])  # old text-send path untouched

    def test_ambiguous_mutation_restarts_unknown_without_duplicate(self):
        self.enqueue("e")
        self.actions.failure = ActionAmbiguous("transport lost")
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "e").status, "unknown")
        with ActionStore(self.root / "actions.sqlite", recover_attempted=True) as recovered:
            self.assertFalse(self.run_once(self.make_bridge(action_store=recovered)))
            self.assertEqual([x.intent_id for x in recovered.list_unknown("42")], ["e"])
        self.assertEqual(len(self.actions.calls), 1)

    def test_cancelled_mutation_marks_unknown_before_propagating(self):
        self.enqueue("e")
        self.actions.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.run_once()
        self.assertEqual(self.action_store.get("42", "e").status, "unknown")
        self.assertEqual(len(self.actions.calls), 1)

    def test_cancelled_preflight_releases_unattempted_lease(self):
        self.enqueue("e")
        self.actions.preflight_failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.run_once()
        self.assertEqual(self.action_store.get("42", "e").status, "queued")
        self.assertEqual(self.actions.calls, [])

    def test_old_binding_cannot_dispatch_mutation(self):
        self.enqueue("e")
        changed = SessionConfig(123, "a" * 32, "+15551234567",
                                allowed_peer_ids=(-100123,), account_id=42,
                                bindings=(Binding(-100123, 40, "c" * 32),))
        self.assertTrue(self.run_once(self.make_bridge(config=changed)))
        self.assertEqual(self.action_store.get("42", "e").status, "failed")
        self.assertEqual(self.actions.preflights, [])
        self.assertEqual(self.actions.calls, [])

    def test_authenticated_account_switch_retires_without_action(self):
        self.enqueue("e")
        async def switched():
            return 99
        self.adapter.get_me = switched
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "e").error_code, "ACCOUNT_CHANGED")
        self.assertEqual(self.actions.preflights, [])
        self.assertEqual(self.actions.calls, [])

    def test_poisoned_first_job_retires_without_starving_next(self):
        bad = self.enqueue("bad")
        path = self.root / "actions" / (bad.payload_ref + ".json")
        path.write_text(json.dumps({"text": "tampered"}), encoding="utf-8")
        self.enqueue("good")
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "bad").status, "failed")
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "good").status, "sent")
        self.assertEqual(len(self.actions.calls), 1)

    def test_invalid_target_retires_but_transient_preflight_releases(self):
        self.enqueue("bad")
        self.actions.preflight_failure = ValueError("other topic")
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "bad").status, "failed")
        self.enqueue("later")
        self.actions.preflight_failure = TimeoutError()
        self.assertFalse(self.run_once())
        held = self.action_store.get("42", "later")
        self.assertEqual(held.status, "queued")
        self.assertGreater(held.next_attempt_at, self.action_store.clock())
        self.actions.preflight_failure = None
        self.assertFalse(self.run_once())  # no three-second retry storm
        self.action_store.clock = lambda: held.next_attempt_at + 1
        self.assertTrue(self.run_once())
        self.assertEqual(len(self.actions.calls), 1)

    def test_incomplete_catalog_during_action_preflight_releases_without_mutation(self):
        self.enqueue("e")
        self.actions.preflight_failure = RuntimeError("forum pagination stopped")
        self.assertFalse(self.run_once())
        self.assertEqual(self.action_store.get("42", "e").status, "queued")
        self.assertEqual(self.actions.calls, [])

    def test_definitive_rpc_refusal_and_flood_wait_fail(self):
        self.enqueue("bad")
        self.actions.failure = ActionRejected("denied")
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "bad").error_code, "RPC_REJECTED")
        self.enqueue("flood")
        self.actions.failure = ActionFloodWait(9)
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "flood").error_code, "FLOOD_WAIT")

    def test_preflight_flood_wait_defers_without_starving_other_actions(self):
        self.enqueue("first")
        self.enqueue("second")
        self.actions.preflight_failure = ActionFloodWait(90)
        self.assertFalse(self.run_once())
        delayed = self.action_store.get("42", "first")
        self.assertEqual(delayed.status, "queued")
        self.assertGreaterEqual(delayed.next_attempt_at - self.action_store.clock(), 89)
        self.actions.preflight_failure = None
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "second").status, "sent")
        self.assertEqual(self.action_store.get("42", "first").status, "queued")
        self.action_store.clock = lambda: delayed.next_attempt_at + 1
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "first").status, "sent")

    def test_native_telethon_flood_wait_preflight_honors_provider_seconds(self):
        from telethon.errors import FloodWaitError
        self.enqueue("native")
        self.actions.preflight_failure = FloodWaitError(None, 180)
        self.assertFalse(self.run_once())
        held = self.action_store.get("42", "native")
        self.assertEqual(held.status, "queued")
        self.assertGreaterEqual(held.next_attempt_at - self.action_store.clock(), 179)
        self.assertFalse(self.run_once())
        self.actions.preflight_failure = None
        self.action_store.clock = lambda: held.next_attempt_at + 1
        self.assertTrue(self.run_once())
        self.assertEqual(self.action_store.get("42", "native").status, "sent")


if __name__ == "__main__":
    unittest.main()


def test_crash_after_work_ref_before_interim_enqueue_replays_interim(tmp_path):
    class Deferred(Host):
        def work(self, ref):
            return PresenceResult("completed", outcome="message", text="final",
                                  work_ref=ref, delivery_reporting_version=1)
    host = Deferred()
    adapter = Adapter()
    config = Config()
    with DurableStore(tmp_path / "store.sqlite") as store:
        bridge = UserSessionBridge(tmp_path, config, adapter, store, host)
        path = bridge.spool_update(event())
        host.result = PresenceResult("completed", outcome="deferred", text="interim",
                                     work_ref="child-crash", turn_ref="parent-crash",
                                     delivery_reporting_version=1)
        def interrupted(*_args, **_kwargs):
            raise OSError("before enqueue")
        bridge._queue_reply = interrupted
        assert asyncio.run(bridge.process_incoming()) == 0
        raw = _read_json(path)
        assert raw["work_ref"] == "child-crash"
        assert raw["pending_interim"]["text"] == "interim"
        restored = UserSessionBridge(tmp_path, config, adapter, store, host)
        assert asyncio.run(restored.process_incoming()) == 1
        assert not path.exists()
        texts = [json.loads((tmp_path / "outgoing" / (r[0] + ".json")).read_text())["text"]
                 for r in store.db.execute("SELECT payload_ref FROM outgoing ORDER BY rowid")]
        assert texts == ["interim", "final"]
