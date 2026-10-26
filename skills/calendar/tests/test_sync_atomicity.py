"""Sync-side atomicity: a provider observation never erases an owner write accepted after the sync read its rows.

Every negative test injects the owner command at the exact seam between the sync's read and its write and asserts
that the injection really ran; the matching positive test is the same scenario without the injection.
In-memory fakes, temporary SQLite stores, no network.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
for path in (SKILL, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import OneAdapterProviders, RecordingAdapter, add_external_calendar, make_context  # noqa: E402
import ops  # noqa: E402
import tools  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, parse_stored  # noqa: E402
from scripts.worker import reconcile_calendar  # noqa: E402


def _day(offset_days, hour=9):
    return datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)


class Seam:
    """Run ``action`` once, right after the first real call of ``obj.name`` returned: the gap between a sync read and its write."""

    def __init__(self, obj, name, action):
        self.fired = []
        real = getattr(obj, name)

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            if not self.fired:
                self.fired.append(name)      # recorded before the action: no re-entry, and the tests assert it
                action()
            return result

        setattr(obj, name, wrapper)


class BeforeTransaction:
    """Inject after fetch, before the whole-feed write boundary.

    The old instance-level lookup hook no longer runs on the bound Store view.
    Separate-connection tests additionally check the held-transaction race.
    """
    def __init__(self, store, action):
        self.fired = []
        real = store.transaction
        @contextmanager
        def wrapped():
            if not self.fired:
                self.fired.append('transaction')
                action()
            with real() as view:
                yield view
        store.transaction = wrapped


class FeedAdapter:
    """A fixed provider listing whose existence probes answer «gone»; ``probes`` proves they were consulted."""

    def __init__(self, rows=(), seen=(), kind="window"):
        self.rows, self.seen, self.kind, self.probes = list(rows), list(seen), kind, []

    def fetch(self, calendar, cursor="", tz=None):
        return self.rows, ("next-token" if self.kind == "google_sync_token" else ""), self.kind, list(self.seen)

    def exists(self, href):
        self.probes.append("exists")
        return False

    def exists_event(self, calendar, external_id):
        self.probes.append("exists_event")
        return False


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.ctx.tz = get_tz("UTC")
        self.external = add_external_calendar(self.ctx.store)
        self.recording = RecordingAdapter()
        self.ctx.providers = OneAdapterProviders(self.recording)
        self.offline = OneAdapterProviders(None)   # an owner write accepted while the provider cannot be reached

    def create(self, **spec):
        spec.setdefault("title", "Event")
        spec.setdefault("start", _day(3).isoformat())
        calendars = spec.pop("calendars", [self.external])
        created = json.loads(tools.cal_create(self.ctx, calendars=calendars, confirm=True, **spec))["event"]
        return self.ctx.store.get_event(created["id"])

    def owner_edit(self, event_id, scope="all"):
        """Accepted owner command: row pending, intent pending, nothing reached the provider."""
        return ops.update_event(self.ctx.store, self.offline, event_id, {"title": "Owner edit"},
                                scope=scope, owner="test", propagate=False)

    def sync(self, adapter):
        return reconcile_calendar(self.ctx.store, self.ctx.providers, adapter,
                                  self.ctx.store.get_calendar(self.external), self.ctx.tz)

    def assert_owner_edit_kept(self, event_id):
        row = self.ctx.store.get_event(event_id)
        self.assertIsNone(row["deleted_at"])
        self.assertEqual((row["title"], row["sync_state"]), ("Owner edit", "pending"))
        self.assertEqual([i["state"] for i in self.ctx.store.intents_for_event(event_id) if i["kind"] == "update"], ["pending"])

    def series(self, with_override=True):
        start = _day(2)
        master = self.create(title="Series", start=start.isoformat(), rrule="FREQ=DAILY;COUNT=5")
        slot = iso_utc(start + timedelta(days=1))
        override = None
        if with_override:
            override = self.ctx.store.insert_event({
                "calendar_id": self.external, "uid": master["uid"], "master_id": master["id"], "recurrence_id": slot,
                "title": "Provider override", "start_utc": slot, "end_utc": iso_utc(start + timedelta(days=1, hours=1)),
                "etag": '"x1"', "origin": "external", "sync_state": "synced"})
        return master, slot, override

    @staticmethod
    def feed_row(master, **over):
        """A provider row: never carries our primary key."""
        row = {k: v for k, v in master.items() if k not in ("id", "calendar_name", "provider", "account_id")}
        return {**row, **over}

    def provider_override(self, master, slot):
        return {"calendar_id": self.external, "uid": master["uid"], "external_id": "", "href": master["href"], "etag": '"x1"',
                "title": "Provider override", "description": "", "location": "", "start_utc": slot,
                "end_utc": iso_utc(parse_stored(slot) + timedelta(hours=1)), "tz": "UTC", "all_day": 0, "rrule": "",
                "exdates": "", "rdates": "", "recurrence_id": slot, "master_external_id": master["external_id"],
                "status": "confirmed", "organizer": "", "attendees_json": "[]", "my_response": "", "reminders_json": "[]",
                "origin": "external", "availability": "busy", "sync_state": "synced", "raw_payload": ""}


class AbsenceRaceTests(SyncCase):
    def test_caldav_head_404_cannot_delete_an_edit_accepted_after_the_window_snapshot(self):
        event = self.create()
        adapter = FeedAdapter()
        seam = Seam(adapter, "exists", lambda: self.owner_edit(event["id"]))
        result = self.sync(adapter)
        self.assertEqual((seam.fired, adapter.probes), (["exists"], ["exists"]))
        self.assertEqual(result[1], 0)
        self.assert_owner_edit_kept(event["id"])

    def test_google_initial_feed_probe_cannot_delete_an_edit_accepted_after_the_snapshot(self):
        event = self.create()
        adapter = FeedAdapter(kind="google_sync_token")
        seam = Seam(adapter, "exists_event", lambda: self.owner_edit(event["id"]))
        result = self.sync(adapter)
        self.assertEqual((seam.fired, adapter.probes), (["exists_event"], ["exists_event"]))
        self.assertEqual(result[1], 0)
        self.assert_owner_edit_kept(event["id"])

    def test_untouched_row_is_still_retired_on_both_feeds(self):
        for kind, probe in (("window", "exists"), ("google_sync_token", "exists_event")):
            with self.subTest(kind=kind):
                event = self.create(title="Gone " + kind)
                adapter = FeedAdapter(kind=kind)
                result = self.sync(adapter)
                self.assertEqual(adapter.probes, [probe])
                self.assertEqual(result[1], 1)
                self.assertTrue(self.ctx.store.get_event(event["id"])["deleted_at"])

    def linked(self):
        source = self.create(calendars=[self.external, DEFAULT_LOCAL_CALENDAR_ID])
        sibling = next(m for m in self.ctx.store.group_masters(source["link_group_id"]) if m["id"] != source["id"])
        return source, sibling

    def test_linked_cascade_cannot_run_over_an_edit_accepted_after_the_snapshot(self):
        source, sibling = self.linked()
        adapter = FeedAdapter()
        seam = Seam(adapter, "exists", lambda: self.owner_edit(source["id"]))
        result = self.sync(adapter)
        self.assertEqual((seam.fired, adapter.probes), (["exists"], ["exists"]))
        self.assertEqual(result[1], 0)
        self.assert_owner_edit_kept(source["id"])
        self.assertEqual([i["kind"] for i in self.ctx.store.intents_for_event(source["id"])], ["create", "update"])   # no delete staged
        self.assertIsNone(self.ctx.store.get_event(sibling["id"])["deleted_at"])
        self.assertEqual([c[0] for c in self.recording.calls if c[0] == "delete"], [])

    def test_linked_cascade_still_runs_for_an_untouched_row(self):
        source, sibling = self.linked()
        adapter = FeedAdapter()
        result = self.sync(adapter)
        self.assertEqual((adapter.probes, result[1]), (["exists"], 1))
        self.assertTrue(self.ctx.store.get_event(source["id"])["deleted_at"])
        self.assertTrue(self.ctx.store.get_event(sibling["id"])["deleted_at"])
        self.assertEqual([c[0] for c in self.recording.calls if c[0] == "delete"], [])  # already absent source is never deleted again

    def test_remote_deletion_cannot_cascade_over_an_edit_of_the_linked_copy(self):
        source, sibling = self.linked()
        adapter = FeedAdapter()
        seam = Seam(adapter, 'exists', lambda: self.owner_edit(sibling['id']))
        result = self.sync(adapter)
        self.assertEqual(seam.fired, ['exists'])
        self.assertEqual(result[1], 0)
        self.assertIsNone(self.ctx.store.get_event(sibling['id'])['deleted_at'])
        self.assertEqual(self.ctx.store.get_event(sibling['id'])['title'], 'Owner edit')

    def test_remote_title_cannot_overwrite_a_copy_edit_accepted_during_fetch(self):
        source, sibling = self.linked()
        adapter = FeedAdapter(rows=[self.feed_row(source, title='Provider title', etag='new')], seen=[source['href']])
        seam = Seam(adapter, 'fetch', lambda: self.owner_edit(sibling['id']))
        self.sync(adapter)
        self.assertEqual(seam.fired, ['fetch'])
        self.assertEqual(self.ctx.store.get_event(source['id'])['title'], 'Provider title')
        self.assertEqual(self.ctx.store.get_event(sibling['id'])['title'], 'Owner edit')

    def test_guard_reads_the_whole_family_inside_its_own_transaction(self):
        master, slot, override = self.series()
        snapshot = self.ctx.store.get_event(master["id"])
        self.owner_edit(f"{master['id']}@{slot}", scope="this")                      # accepted after `snapshot` was read
        self.assertEqual(self.ctx.store.provider_absence_blocker(master["id"], snapshot), "unsettled")
        self.assertFalse(self.ctx.store.retire_provider_absent(master["id"], snapshot))
        self.assertIsNone(self.ctx.store.get_event(master["id"])["deleted_at"])
        self.assertIsNone(self.ctx.store.get_event(override["id"])["deleted_at"])

    def test_absence_is_bound_to_the_identity_it_was_observed_for(self):
        event = self.create()
        snapshot = self.ctx.store.get_event(event["id"])
        self.ctx.store.update_event(event["id"], {"href": "https://example.test/rebound.ics"})
        self.assertEqual(self.ctx.store.provider_absence_blocker(event["id"], snapshot), "rebound")
        self.assertFalse(self.ctx.store.retire_provider_absent(event["id"], snapshot))
        self.assertIsNone(self.ctx.store.get_event(event["id"])["deleted_at"])

    def test_settled_family_is_retired_together(self):
        master, slot, override = self.series()
        snapshot = self.ctx.store.get_event(master["id"])
        self.assertEqual(self.ctx.store.provider_absence_blocker(master["id"], snapshot), "")
        self.assertTrue(self.ctx.store.retire_provider_absent(master["id"], snapshot))
        self.assertTrue(self.ctx.store.get_event(master["id"])["deleted_at"])
        self.assertTrue(self.ctx.store.get_event(override["id"])["deleted_at"])


class ExceptionRetirementRaceTests(SyncCase):
    def refreshed_sync(self, master):
        remote = self.feed_row(master, etag='"remote-2"')   # the whole CalDAV resource was re-read and lists no override
        return self.sync(FeedAdapter(rows=[remote], seen=[master["href"]]))

    def test_refreshed_series_cannot_retire_an_override_the_owner_just_edited(self):
        master, slot, override = self.series()
        seam = BeforeTransaction(self.ctx.store, lambda: self.owner_edit(f"{master['id']}@{slot}", scope="this"))
        self.refreshed_sync(master)
        self.assertEqual(seam.fired, ["transaction"])
        kept = self.ctx.store.get_event(override["id"])
        self.assertIsNone(kept["deleted_at"])
        self.assertEqual((kept["title"], kept["sync_state"]), ("Owner edit", "pending"))
        self.assertEqual([i["state"] for i in self.ctx.store.intents_for_event(override["id"])], ["pending"])

    def test_refreshed_series_still_retires_an_untouched_stale_override(self):
        master, slot, override = self.series()
        self.refreshed_sync(master)
        self.assertTrue(self.ctx.store.get_event(override["id"])["deleted_at"])
        self.assertEqual(self.ctx.store.exceptions_for(master["id"]), [])


class ExceptionImportRaceTests(SyncCase):

    def test_import_cannot_attach_a_stale_slot_after_an_owner_series_move(self):
        master, slot, _ = self.series(with_override=False)
        seam = BeforeTransaction(self.ctx.store, lambda: ops.update_event(
            self.ctx.store, self.offline, master['id'],
            {'start_utc': iso_utc(parse_stored(master['start_utc']) + timedelta(hours=1))}, scope='all'))
        self.sync(self.feed(master, slot))
        self.assertEqual(seam.fired, ['transaction'])
        self.assertEqual(self.ctx.store.get_event(master['id'])['sync_state'], 'pending')
        self.assertEqual(self.ctx.store.exceptions_for(master['id']), [])

    def feed(self, master, slot):
        return FeedAdapter(rows=[self.feed_row(master), self.provider_override(master, slot)], seen=[master["href"]])

    def test_owner_override_created_after_the_lookup_is_not_duplicated_by_the_import(self):
        master, slot, _ = self.series(with_override=False)
        seam = BeforeTransaction(self.ctx.store, lambda: self.owner_edit(f"{master['id']}@{slot}", scope="this"))
        result = self.sync(self.feed(master, slot))
        self.assertEqual(seam.fired, ["transaction"])
        rows = self.ctx.store.exceptions_for(master["id"])
        self.assertEqual([(r["title"], r["sync_state"]) for r in rows], [("Owner edit", "pending")])
        self.assertEqual(result[0], 0)

    def test_import_into_a_series_the_owner_just_deleted_is_refused(self):
        master, slot, _ = self.series(with_override=False)
        seam = BeforeTransaction(self.ctx.store,
                    lambda: ops.delete_event(self.ctx.store, self.offline, master["id"], scope="all", owner="test"))
        result = self.sync(self.feed(master, slot))
        self.assertEqual(seam.fired, ["transaction"])
        self.assertEqual(self.ctx.store.get_event(master["id"])["sync_state"], "pending_delete")   # the injection took effect
        self.assertEqual(self.ctx.store.exceptions_for(master["id"]), [])
        self.assertEqual(result[0], 0)

    def test_import_into_a_live_series_inserts_the_override_once(self):
        master, slot, _ = self.series(with_override=False)
        feed = self.feed(master, slot)
        result = self.sync(feed)
        rows = self.ctx.store.exceptions_for(master["id"])
        self.assertEqual([(r["title"], r["etag"]) for r in rows], [("Provider override", '"x1"')])
        self.assertEqual(result[0], 1)
        self.sync(feed)
        self.assertEqual(len(self.ctx.store.exceptions_for(master["id"])), 1)


class SnapshotOnClosedSeriesTests(SyncCase):
    def test_snapshot_cannot_revive_an_override_of_an_owner_deleted_series(self):
        master, slot, override = self.series()
        ops.delete_event(self.ctx.store, self.offline, master["id"], scope="all", owner="test")
        self.assertTrue(self.ctx.store.get_event(override["id"])["deleted_at"])               # the owner's delete retired it
        self.assertFalse(self.ctx.store.apply_provider_snapshot(override["id"], {"title": "Imported", "sync_state": "synced"}))
        self.assertTrue(self.ctx.store.get_event(override["id"])["deleted_at"])

    def test_snapshot_still_updates_an_override_of_a_live_series(self):
        master, slot, override = self.series()
        self.assertTrue(self.ctx.store.apply_provider_snapshot(override["id"], {"title": "Imported", "sync_state": "synced"}))
        self.assertEqual(self.ctx.store.get_event(override["id"])["title"], "Imported")


if __name__ == "__main__":
    unittest.main()
