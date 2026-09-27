"""Calendar → Host Service notification contract and no-blind-retry policy."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

import reminders as rem  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, now_utc  # noqa: E402
from store import Store  # noqa: E402


class Reply:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.data


class NotifyContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.now = now_utc().replace(microsecond=0)
        self.tz = get_tz("UTC")

    def _event(self, title="Meeting", minutes=10):
        event = self.store.insert_event({
            "calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": title,
            "start_utc": iso_utc(self.now + timedelta(minutes=minutes)),
            "end_utc": iso_utc(self.now + timedelta(hours=1)),
        })
        self.store.schedule_reminder(event["id"], event["start_utc"], 15,
                                     iso_utc(self.now - timedelta(minutes=5)),
                                     rem.notice_id_for(event["id"], event["start_utc"], 15))
        return event

    def test_request_shape_and_host_acceptance_only(self):
        sent = []

        def urlopen(req, timeout):
            sent.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
            return Reply({"notify_version": 1} if req.get_method() == "GET" else
                         {"ok": True, "ts": "2026-09-27T13:00:00Z", "chat_id": 1})

        with patch("urllib.request.urlopen", side_effect=urlopen):
            channel = rem.NotifyChannel("http://127.0.0.1:8767", "test-token")
            self.assertEqual(channel.state(), "ready")
            self.assertEqual(channel.send("cal:one", "Meeting")[0], "sent")
        self.assertEqual(sent[1], ("POST", "http://127.0.0.1:8767/notify", {"key": "cal:one", "text": "Meeting"}))

    def test_unknown_success_or_lost_response_is_not_retried(self):
        event = self._event()
        channel = rem.NotifyChannel("http://127.0.0.1:8767", "test-token")
        channel._state = "ready"
        with patch.object(channel, "_request", side_effect=TimeoutError("lost after send")) as request:
            stats = rem.deliver_due(self.store, channel, self.tz, now=self.now)
            rem.deliver_due(self.store, channel, self.tz, now=self.now + timedelta(minutes=1))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(self.store.unknown_reminder_count(), 1)
        self.assertEqual(self.store.due_reminders(self.now + timedelta(minutes=1)), [])
        self.assertEqual(self.store.upcoming_reminders(self.now)[0]["state"], "unknown")
        channel._state = "ready"
        with patch.object(channel, "_request", return_value={"ok": False, "ts": "t", "chat_id": 1}):
            self.assertEqual(channel.send("cal:other", "Meeting")[0], "unknown")

    def test_outage_only_affects_the_attempted_reminder(self):
        self._event(title="First")
        self._event(title="Second")
        channel = rem.NotifyChannel("http://127.0.0.1:8767", "test-token")
        channel._state = "ready"
        with patch.object(channel, "_request", side_effect=TimeoutError("response lost")) as request:
            stats = rem.deliver_due(self.store, channel, self.tz, now=self.now)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["no_channel"], 1)
        self.assertEqual(len(self.store.due_reminders(self.now)), 1)

    def test_connection_refused_before_send_keeps_both_reminders(self):
        self._event(title="First")
        self._event(title="Second")
        channel = rem.NotifyChannel("http://127.0.0.1:8767", "test-token")
        channel._state = "ready"
        with patch.object(channel, "_request", side_effect=urllib.error.URLError(ConnectionRefusedError("refused"))) as request:
            stats = rem.deliver_due(self.store, channel, self.tz, now=self.now)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(stats["unknown"], 0)
        self.assertEqual(stats["no_channel"], 2)
        self.assertEqual(len(self.store.due_reminders(self.now)), 2)

    def test_known_unavailable_channel_never_reserves_before_noop(self):
        self._event()
        channel = rem.NotifyChannel("http://127.0.0.1:8767", "test-token")
        channel._state = "no_route"
        with patch.object(self.store, "reserve_reminder_send", side_effect=AssertionError("must not reserve")):
            stats = rem.deliver_due(self.store, channel, self.tz, now=self.now)
        self.assertEqual(stats["no_channel"], 1)
        self.assertEqual(self.store.unknown_reminder_count(), 0)

    def test_http_500_is_unknown_and_stops_subsequent_attempts(self):
        self._event(title="First")
        self._event(title="Second")
        channel = rem.NotifyChannel("http://127.0.0.1:8767", "test-token")
        channel._state = "ready"
        with patch.object(channel, "_request", side_effect=rem._HttpError(500, "server error")) as request:
            stats = rem.deliver_due(self.store, channel, self.tz, now=self.now)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["no_channel"], 1)

    def test_explicit_nonacceptance_can_retry_and_missing_grant_is_visible(self):
        channel = rem.NotifyChannel("http://127.0.0.1:8767", "test-token")
        channel._state = "ready"
        with patch.object(channel, "_request", side_effect=rem._HttpError(503, "write failed")):
            self.assertEqual(channel.send("cal:a", "text")[0], "retry")
        with patch.object(channel, "_request", side_effect=rem._HttpError(403, "no grant")):
            self.assertEqual(channel.send("cal:a", "text")[0], "no_grant")
        self.assertEqual(channel.state(), "no_grant")

    def test_catchup_batches_are_complete_and_under_host_limit(self):
        for n in range(15):
            self._event(title=f"Meeting {n} " + "x" * 120)

        class Channel:
            def __init__(self):
                self.sent = []

            def send(self, key, text):
                self.sent.append((key, text))
                return "sent", "host accepted"

        channel = Channel()
        stats = rem.deliver_due(self.store, channel, self.tz, now=self.now + timedelta(minutes=11))
        self.assertEqual(stats["batched"], 15)
        self.assertGreater(len(channel.sent), 1)
        self.assertTrue(all(len(text) <= 1000 for _, text in channel.sent))
        for n in range(15):
            self.assertEqual(sum(f"Meeting {n} x" in text for _, text in channel.sent), 1)

    def test_due_snapshot_does_not_hide_a_short_event_behind_fifty_rows(self):
        for n in range(55):
            self._event(title=f"Older {n}")
        short = self._event(title="Short event")

        class Channel:
            def send(self, key, text):
                return "sent", "host accepted"

        stats = rem.deliver_due(self.store, Channel(), self.tz, now=self.now)
        self.assertEqual(stats["sent"], 56)
        self.assertFalse(any(r["event_id"] == short["id"] for r in self.store.due_reminders(self.now)))

    def test_crash_after_batch_acceptance_never_reposts_a_subset(self):
        for n in range(3):
            self._event(title=f"Crash {n}")

        class Channel:
            def __init__(self):
                self.calls = 0

            def send(self, key, text):
                self.calls += 1
                return "sent", "host accepted"

        channel = Channel()
        with patch.object(rem, "_record", side_effect=RuntimeError("crash between batch row writes")):
            with self.assertRaises(RuntimeError):
                rem.deliver_due(self.store, channel, self.tz, now=self.now + timedelta(minutes=11))
        self.assertEqual(channel.calls, 1)
        self.assertEqual(self.store.unknown_reminder_count(), 3)
        rem.deliver_due(self.store, channel, self.tz, now=self.now + timedelta(minutes=12))
        self.assertEqual(channel.calls, 1)


if __name__ == "__main__":
    unittest.main()
