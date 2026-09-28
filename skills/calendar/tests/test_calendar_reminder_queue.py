"""Reminder queue consistency: explicit «no reminders» per event, provider-side offset/exception changes
reaching the queue, and a replanned member not holding back a catch-up batch.

All persistence lives in temporary SQLite directories; provider and Host Service traffic are in-memory fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPTS = os.path.join(SKILL, "scripts")
for path in (SKILL, HERE, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import Channel, Request, RouteAPI, add_external_calendar, make_context  # noqa: E402
from test_calendar_round5 import FakeFetchAdapter  # noqa: E402

import reminders as rem  # noqa: E402
import ops  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
import worker as calendar_worker  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, iso_utc, now_utc  # noqa: E402
from providers import row_to_ics  # noqa: E402
from providers_google import GoogleAdapter, gevent_to_row, row_to_gevent  # noqa: E402
from store import Store  # noqa: E402

UTC = timezone.utc


def plan(ctx, now):
    return rem.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=now)


def waiting(ctx, now, event_id=None):
    """(event_id, occurrence_start_utc, offset_min) of rows still waiting to be attempted."""
    return sorted(
        (row["event_id"], row["occurrence_start_utc"], row["offset_min"])
        for row in ctx.store.upcoming_reminders(now, limit=200)
        if row["state"] in ("scheduled", "no_channel") and (event_id is None or row["event_id"] == event_id)
    )


def external_event(calendar_id, name, start, reminders="[15]", **extra):
    return {
        "calendar_id": calendar_id, "uid": f"{name}@example.test", "external_id": f"remote-{name}",
        "href": f"https://caldav.yandex.ru/calendars/round2/{name}.ics", "etag": '"e1"', "title": name,
        "start_utc": iso_utc(start), "end_utc": iso_utc(start + timedelta(hours=1)), "tz": "UTC",
        "reminders_json": reminders, "origin": "external", "sync_state": "synced", **extra,
    }


def sync(ctx, calendar_id, rows):
    adapter = FakeFetchAdapter(rows, [row["href"] for row in rows if row.get("href")])
    return calendar_worker.reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(calendar_id), UTC)


class ExplicitNoRemindersTests(unittest.TestCase):
    def test_create_distinguishes_omitted_reminders_from_explicit_empty(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        tools.cal_reminders(ctx, action="set_default", offsets=[10], confirm=True)
        inherited = json.loads(tools.cal_create(ctx, title="Inherit", start=(now + timedelta(hours=2)).isoformat(), confirm=True))["event"]
        quiet = json.loads(tools.cal_create(ctx, title="Quiet", start=(now + timedelta(hours=3)).isoformat(), reminders=[], confirm=True))["event"]
        self.assertEqual([r[2] for r in waiting(ctx, now, inherited["id"])], [10])
        self.assertEqual(waiting(ctx, now, quiet["id"]), [])
        self.assertEqual(ctx.store.get_event(quiet["id"])["reminders_json"], json.dumps("off"))

    def test_rule_replan_error_does_not_delete_waiting_reminders(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        saved = ctx.store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": "Keep",
                                        "start_utc": iso_utc(now + timedelta(hours=2)),
                                        "end_utc": iso_utc(now + timedelta(hours=3)), "reminders_json": "[15]"})
        plan(ctx, now)
        self.assertEqual(len(waiting(ctx, now, saved["id"])), 1)
        with patch.object(ctx, "occurrences", side_effect=RuntimeError("source unavailable")):
            warning = tools._plan_reminders(ctx)
        self.assertIn("source unavailable", warning)
        self.assertEqual(len(waiting(ctx, now, saved["id"])), 1)

    def test_google_import_keeps_explicit_none_distinct_from_default(self):
        base = {"id": "g-1", "iCalUID": "g-1@example.test", "summary": "Google event",
                "start": {"dateTime": "2026-10-01T09:00:00+00:00"},
                "end": {"dateTime": "2026-10-01T10:00:00+00:00"}}
        off = gevent_to_row({**base, "reminders": {"useDefault": False, "overrides": []}}, "google:test", UTC)
        default = gevent_to_row({**base, "reminders": {"useDefault": True}}, "google:test", UTC)
        self.assertEqual(off["reminders_json"], json.dumps("off"))
        self.assertEqual(default["reminders_json"], "[]")
        # A remote edit that turns defaults back on must override an earlier local off state.
        self.assertEqual(calendar_worker._imported_reminders(off, default["reminders_json"], "google"), "[]")
        # Calendar-owned delivery mutes Google alarms. A later feed echo of that mute
        # must not erase this event's local offset, but a positive new offset wins.
        own = {**off, "reminders_json": "[15]"}
        self.assertEqual(calendar_worker._imported_reminders(own, off["reminders_json"], "google", mode_on=True), "[15]")
        self.assertEqual(calendar_worker._imported_reminders(own, "[5]", "google", mode_on=True), "[5]")

    def test_title_only_google_exception_patch_does_not_change_provider_alarms(self):
        adapter = GoogleAdapter("owner@example.test", "token", "refresh", datetime.now(UTC) + timedelta(days=1), "client", "", None)
        adapter.requests = []
        def fake_request(method, path, params=None, body=None, headers=None, _retry=True):
            adapter.requests.append((method, path, params, body, headers))
            return 200, {}, {"id": path.rsplit("/", 1)[-1]}
        adapter._request = fake_request
        event = {"id": "exc", "master_id": "master", "external_id": "google-instance", "uid": "series@test",
                 "title": "Edited title", "start_utc": "2026-10-01T09:00:00+00:00",
                 "end_utc": "2026-10-01T10:00:00+00:00", "tz": "UTC", "reminders_json": "[]",
                 "attendees_json": "[]"}
        calendar = {"external_id": "primary"}
        adapter.update(calendar, event, "", {"changes": {"title": "Edited title"}})
        self.assertNotIn("reminders", adapter.requests[-1][3])
        adapter.update(calendar, event, "", {"changes": {"reminders": "default"}})
        self.assertIn("reminders", adapter.requests[-1][3])

    def test_empty_update_silences_one_event_and_default_restores_the_rule(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        tools.cal_reminders(ctx, action="set_default", offsets=[10], confirm=True)
        keep = json.loads(tools.cal_create(ctx, title="Follows rule", start=(now + timedelta(hours=2)).isoformat(), confirm=True))["event"]
        quiet = json.loads(tools.cal_create(ctx, title="Quiet", start=(now + timedelta(hours=3)).isoformat(), confirm=True))["event"]
        self.assertEqual([r[2] for r in waiting(ctx, now, quiet["id"])], [10])

        updated = json.loads(tools.cal_update(ctx, id=quiet["id"], reminders=[], confirm=True))
        self.assertEqual((updated["event"]["reminders"], updated["event"].get("reminders_off")), ([], True))
        self.assertEqual(waiting(ctx, now, quiet["id"]), [])
        # an untouched event keeps the stored default '[]' = «by the rules»: existing events do not change
        self.assertEqual(ctx.store.get_event(keep["id"])["reminders_json"], "[]")
        self.assertEqual([r[2] for r in waiting(ctx, now, keep["id"])], [10])
        self.assertNotIn("reminders_off", json.loads(tools.cal_events(ctx, id=keep["id"]))["event"])
        json.loads(tools.cal_update(ctx, id=keep["id"], title="Renamed", reminders="", confirm=True))   # blank = not given
        self.assertEqual(ctx.store.get_event(keep["id"])["reminders_json"], "[]")

        json.loads(tools.cal_update(ctx, id=quiet["id"], reminders="default", confirm=True))
        self.assertEqual(ctx.store.get_event(quiet["id"])["reminders_json"], "[]")
        self.assertEqual([r[2] for r in waiting(ctx, now, quiet["id"])], [10])

    def test_widget_cleared_field_still_means_by_the_rules(self):
        ctx = make_context()
        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        now = now_utc().replace(microsecond=0)
        tools.cal_reminders(ctx, action="set_default", offsets=[10], confirm=True)
        event = json.loads(tools.cal_create(ctx, title="Widget", start=(now + timedelta(hours=2)).isoformat(), reminders=[30], confirm=True))["event"]
        asyncio.run(api.routes["event/update"](Request({"id": event["id"], "reminders": [], "reminders_edited": True})))
        self.assertEqual(ctx.store.get_event(event["id"])["reminders_json"], "[]")
        self.assertEqual([r[2] for r in waiting(ctx, now, event["id"])], [10])

    def test_widget_new_event_with_blank_reminder_field_inherits_rules(self):
        ctx = make_context()
        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        now = now_utc().replace(microsecond=0)
        tools.cal_reminders(ctx, action="set_default", offsets=[10], confirm=True)
        reply = asyncio.run(api.routes["event"](Request({"title": "Widget new", "start": (now + timedelta(hours=2)).isoformat(),
                                                               "reminders": []})))
        event_id = json.loads(reply.body)["event"]["id"]
        self.assertEqual(ctx.store.get_event(event_id)["reminders_json"], "[]")
        self.assertEqual([r[2] for r in waiting(ctx, now, event_id)], [10])

    def test_providers_write_explicit_none_as_no_alarms(self):
        event = {"uid": "quiet@example.test", "title": "Quiet", "start_utc": "2026-10-01T09:00:00+00:00",
                 "end_utc": "2026-10-01T10:00:00+00:00", "tz": "UTC", "all_day": 0, "attendees_json": "[]", "raw_payload": ""}
        off = {**event, "reminders_json": json.dumps("off")}
        self.assertEqual(row_to_gevent(off)["reminders"], {"useDefault": False, "overrides": []})
        self.assertEqual(row_to_gevent({**event, "reminders_json": "[]"})["reminders"], {"useDefault": True})
        self.assertNotIn("VALARM", row_to_ics(off))
        self.assertIn("VALARM", row_to_ics({**event, "reminders_json": "[20]"}))

    def test_provider_feed_without_alarms_keeps_local_none_until_an_alarm_appears(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store, "quiet-sync")
        rem.set_mode(ctx.store, external, True)
        start = now_utc().replace(microsecond=0) + timedelta(hours=2)
        row = external_event(external, "quiet-sync", start, reminders=json.dumps("off"))
        saved = ctx.store.insert_event(row)
        sync(ctx, external, [{**row, "etag": '"e2"', "title": "Renamed remotely", "reminders_json": "[]"}])
        self.assertEqual(ctx.store.get_event(saved["id"])["reminders_json"], json.dumps("off"))
        sync(ctx, external, [{**row, "etag": '"e3"', "reminders_json": "[10]"}])
        self.assertEqual(ctx.store.get_event(saved["id"])["reminders_json"], "[10]")

    def test_muted_caldav_echo_keeps_selected_local_offset(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store, "muted-caldav")
        rem.set_mode(ctx.store, external, True)
        start = now_utc().replace(microsecond=0) + timedelta(hours=2)
        row = external_event(external, "muted-caldav", start, reminders="[15]")
        saved = ctx.store.insert_event(row)
        # The provider echoes a no-VALARM payload after calendar-owned delivery muted it.
        sync(ctx, external, [{**row, "etag": '"e2"', "title": "Edited remotely", "reminders_json": "[]"}])
        self.assertEqual(ctx.store.get_event(saved["id"])["reminders_json"], "[15]")
        plan(ctx, now_utc())
        self.assertEqual([r[2] for r in waiting(ctx, now_utc(), saved["id"])], [15])

    def test_exception_without_own_offsets_inherits_master_explicit_off(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        tools.cal_reminders(ctx, action="set_default", offsets=[10], confirm=True)
        start = now + timedelta(hours=2)
        master = ctx.store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": "Series", "rrule": "FREQ=DAILY",
                                         "start_utc": iso_utc(start), "end_utc": iso_utc(start + timedelta(hours=1)), "reminders_json": "[]"})
        # Editing only the title creates an exception, not a frozen reminder override.
        json.loads(tools.cal_update(ctx, id=f'{master["id"]}@{iso_utc(start)}', title="Exception", scope="this", confirm=True))
        exceptions = ctx.store.exceptions_for(master["id"])
        self.assertEqual(len(exceptions), 1)
        self.assertEqual(exceptions[0]["reminders_json"], "[]")
        json.loads(tools.cal_update(ctx, id=master["id"], reminders=[], scope="all", confirm=True))
        self.assertEqual(ctx.store.get_event(master["id"])["reminders_json"], json.dumps("off"))
        reason, effective = rem._occurrence_state(ctx.store, ctx.store.get_event(master["id"]), start, now, owner_tz=UTC)
        self.assertEqual(reason, "")
        self.assertEqual(effective["reminders_json"], json.dumps("off"))
        plan(ctx, now)
        self.assertEqual(waiting(ctx, now, master["id"]), [])

    def test_exception_moved_from_outside_window_uses_master_offset(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        tools.cal_reminders(ctx, action="set_default", offsets=[10], confirm=True)
        original = now + timedelta(days=10)
        moved = now + timedelta(hours=2)
        master = ctx.store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": "Distant series",
                                         "rrule": "FREQ=DAILY", "start_utc": iso_utc(original),
                                         "end_utc": iso_utc(original + timedelta(hours=1)), "reminders_json": "[15]"})
        ctx.store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "master_id": master["id"],
                                "recurrence_id": iso_utc(original), "title": "Moved occurrence",
                                "start_utc": iso_utc(moved), "end_utc": iso_utc(moved + timedelta(hours=1)),
                                "reminders_json": "[]"})
        occurrences = ctx.occurrences(now, now + timedelta(days=1))
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["reminders_json"], "[15]")
        plan(ctx, now)
        self.assertEqual([r[2] for r in waiting(ctx, now, master["id"])], [15])


class ProviderChangeQueueTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.external = add_external_calendar(self.ctx.store, "offsets")
        rem.set_mode(self.ctx.store, self.external, True)   # «напоминает Уроборос» for this external calendar
        self.now = now_utc().replace(microsecond=0)

    def test_provider_offset_change_replaces_the_waiting_notice(self):
        row = external_event(self.external, "offsets", self.now + timedelta(hours=2), reminders="[30]")
        saved = self.ctx.store.insert_event(row)
        plan(self.ctx, self.now)
        self.assertEqual([r[2] for r in waiting(self.ctx, self.now, saved["id"])], [30])

        sync(self.ctx, self.external, [{**row, "etag": '"e2"', "reminders_json": "[5]"}])
        plan(self.ctx, self.now)
        self.assertEqual([r[2] for r in waiting(self.ctx, self.now, saved["id"])], [5])

    def test_due_notice_whose_offset_was_removed_is_skipped_at_send(self):
        row = external_event(self.external, "late-change", self.now + timedelta(minutes=40), reminders="[30]")
        saved = self.ctx.store.insert_event(row)
        plan(self.ctx, self.now)
        sync(self.ctx, self.external, [{**row, "etag": '"e2"', "reminders_json": "[5]"}])   # sync ran after this tick's plan
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=self.now + timedelta(minutes=10))
        self.assertEqual((channel.sent, stats["skipped"]), ([], 1))
        plan(self.ctx, self.now + timedelta(minutes=10))
        self.assertEqual([r[2] for r in waiting(self.ctx, self.now, saved["id"])], [5])

    def test_pending_external_delete_cannot_replan_or_deliver_a_notice(self):
        row = external_event(self.external, "deleting", self.now + timedelta(minutes=20))
        saved = self.ctx.store.insert_event(row)
        plan(self.ctx, self.now)
        self.assertEqual(len(waiting(self.ctx, self.now, saved["id"])), 1)
        self.ctx.store.update_event(saved["id"], {"sync_state": "pending_delete"})
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=self.now + timedelta(minutes=5))
        self.assertEqual((channel.sent, stats["skipped"]), ([], 1))
        plan(self.ctx, self.now)
        self.assertEqual(waiting(self.ctx, self.now, saved["id"]), [])

    def test_external_exception_reminder_change_is_imported_and_replanned(self):
        start = self.now + timedelta(hours=2)
        master_row = external_event(self.external, "series", start, rrule="FREQ=DAILY")
        master = self.ctx.store.insert_event(master_row)
        exception_row = {**master_row, "rrule": "", "external_id": "", "href": "", "etag": '"x1"', "title": "Moved title",
                         "recurrence_id": iso_utc(start), "master_id": master["id"]}
        self.ctx.store.insert_event(exception_row)
        plan(self.ctx, self.now)
        self.assertEqual(waiting(self.ctx, self.now, master["id"]), [(master["id"], iso_utc(start), 15)])

        remote_exception = {**exception_row, "etag": '"x2"', "reminders_json": "[5]", "master_external_id": master_row["external_id"]}
        remote_exception.pop("master_id")
        sync(self.ctx, self.external, [master_row, remote_exception])
        plan(self.ctx, self.now)
        self.assertEqual(waiting(self.ctx, self.now, master["id"]), [(master["id"], iso_utc(start), 5)])
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=start - timedelta(minutes=5))
        self.assertEqual((stats["sent"], len(channel.sent)), (1, 1))
        self.assertIn("Moved title", channel.sent[0][1])

    def test_moved_and_cancelled_events_leave_the_queue_but_uncertain_history_stays(self):
        moved_row = external_event(self.external, "moved", self.now + timedelta(hours=2))
        gone_row = external_event(self.external, "gone", self.now + timedelta(hours=3))
        unsure_row = external_event(self.external, "unsure", self.now + timedelta(minutes=10))
        moved = self.ctx.store.insert_event(moved_row)
        gone = self.ctx.store.insert_event(gone_row)
        unsure = self.ctx.store.insert_event(unsure_row)
        plan(self.ctx, self.now)
        unsure_ids = [r["id"] for r in self.ctx.store.due_reminders(self.now) if r["event_id"] == unsure["id"]]
        self.ctx.store.reserve_reminder_send(unsure_ids)   # a POST whose outcome was lost (2B)
        self.assertEqual(self.ctx.store.unknown_reminder_count(), 1)

        new_start = self.now + timedelta(hours=5)
        sync(self.ctx, self.external, [
            {**moved_row, "etag": '"e2"', "start_utc": iso_utc(new_start), "end_utc": iso_utc(new_start + timedelta(hours=1))},
            {**gone_row, "etag": '"e2"', "status": "cancelled"},
            {**unsure_row, "etag": '"e2"', "title": "Unsure renamed"},
        ])
        plan(self.ctx, self.now)
        self.assertEqual(waiting(self.ctx, self.now), [(moved["id"], iso_utc(new_start), 15)])
        self.assertEqual(waiting(self.ctx, self.now, gone["id"]), [])
        self.assertEqual(self.ctx.store.unknown_reminder_count(), 1)
        channel = Channel()
        rem.deliver_due(self.ctx.store, channel, UTC, now=self.now + timedelta(minutes=1))
        self.assertEqual(channel.sent, [])   # the uncertain notice is never re-sent automatically


class RecurringOccurrenceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.now = now_utc().replace(microsecond=0)
        self.first = self.now + timedelta(hours=2)
        self.second = self.first + timedelta(days=1)
        self.master = self.ctx.store.insert_event({
            "calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": "Original", "rrule": "FREQ=DAILY",
            "start_utc": iso_utc(self.first), "end_utc": iso_utc(self.first + timedelta(hours=1)),
            "reminders_json": "[15]",
        })

    def move(self, original, effective, title):
        return self.ctx.store.insert_event({
            "calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "master_id": self.master["id"],
            "recurrence_id": iso_utc(original), "title": title, "start_utc": iso_utc(effective),
            "end_utc": iso_utc(effective + timedelta(hours=1)), "reminders_json": "[]",
        })

    def test_two_distinct_occurrences_moved_to_same_effective_time_both_deliver(self):
        target = self.first + timedelta(days=3, hours=2)
        self.move(self.first, target, "First move")
        self.move(self.second, target, "Second move")
        planning_time = target - timedelta(hours=1)
        plan(self.ctx, planning_time)
        rows = [r for r in self.ctx.store.upcoming_reminders(planning_time, limit=200)
                if r["event_id"] == self.master["id"] and r["occurrence_start_utc"] == iso_utc(target)]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["recurrence_id"] for r in rows}, {iso_utc(self.first), iso_utc(self.second)})
        self.assertEqual(len({r["notice_id"] for r in rows}), 2)
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=target - timedelta(minutes=15))
        self.assertEqual((stats["sent"], len(channel.sent)), (2, 2))
        self.assertEqual({"First move", "Second move"}, {title for title in ("First move", "Second move")
                         if any(title in text for _, text in channel.sent)})

    def test_original_slot_and_moved_exception_exchange_times(self):
        self.move(self.first, self.second, "First at second")
        self.move(self.second, self.first, "Second at first")
        plan(self.ctx, self.now)
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=self.first - timedelta(minutes=15))
        self.assertEqual((stats["sent"], stats["skipped"]), (1, 0))
        self.assertIn("Second at first", channel.sent[0][1])
        plan(self.ctx, self.second - timedelta(hours=1))
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=self.second - timedelta(minutes=15))
        self.assertEqual((stats["sent"], stats["skipped"]), (1, 0))
        self.assertIn("First at second", channel.sent[1][1])

    def test_upgrade_preserves_legacy_uncertain_moved_notice_without_resending(self):
        target = self.first + timedelta(days=3)
        self.move(self.first, target, "Moved")
        old_id = rem.notice_id_for(self.master["id"], iso_utc(target), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(target), 15,
                                         iso_utc(target - timedelta(minutes=15)), old_id)
        old_row = next(r for r in self.ctx.store.upcoming_reminders(self.now, 200) if r["notice_id"] == old_id)
        self.ctx.store.mark_reminder(old_row["id"], "unknown")
        planning_time = target - timedelta(hours=1)
        plan(self.ctx, planning_time)
        self.assertFalse(any(r["occurrence_start_utc"] == iso_utc(target)
                             for r in self.ctx.store.due_reminders(target - timedelta(minutes=15))))
        channel = Channel()
        rem.deliver_due(self.ctx.store, channel, UTC, now=target - timedelta(minutes=15))
        self.assertEqual(channel.sent, [])
        self.assertEqual(self.ctx.store.unknown_reminder_count(), 1)

    def test_upgrade_replans_waiting_legacy_slot_when_another_occurrence_moves_into_it(self):
        old_id = rem.notice_id_for(self.master["id"], iso_utc(self.first), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(self.first), 15,
                                         iso_utc(self.first - timedelta(minutes=15)), old_id)
        self.move(self.second, self.first, "Second at first")
        plan(self.ctx, self.now)
        rows = [r for r in self.ctx.store.upcoming_reminders(self.now, limit=200)
                if r["event_id"] == self.master["id"] and r["occurrence_start_utc"] == iso_utc(self.first)]
        self.assertEqual({r["recurrence_id"] for r in rows}, {iso_utc(self.first), iso_utc(self.second)})
        self.assertNotIn(old_id, {r["notice_id"] for r in rows})
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=self.first - timedelta(minutes=15))
        self.assertEqual((stats["sent"], len(channel.sent)), (2, 2))
        self.assertTrue(any("Second at first" in text for _, text in channel.sent))

    def test_upgrade_does_not_resend_a_legacy_sent_unmoved_occurrence(self):
        old_id = rem.notice_id_for(self.master["id"], iso_utc(self.first), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(self.first), 15,
                                         iso_utc(self.first - timedelta(minutes=15)), old_id)
        row = next(r for r in self.ctx.store.upcoming_reminders(self.now, 200) if r["notice_id"] == old_id)
        self.ctx.store.mark_reminder(row["id"], "sent")
        plan(self.ctx, self.now)
        channel = Channel()
        rem.deliver_due(self.ctx.store, channel, UTC, now=self.first - timedelta(minutes=15))
        self.assertEqual(channel.sent, [])

    def test_upgrade_retires_due_legacy_no_channel_row_before_new_identity(self):
        old_id = rem.notice_id_for(self.master["id"], iso_utc(self.first), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(self.first), 15,
                                         iso_utc(self.first - timedelta(minutes=15)), old_id)
        row = next(r for r in self.ctx.store.upcoming_reminders(self.now, 200) if r["notice_id"] == old_id)
        self.ctx.store.mark_reminder(row["id"], "no_channel")
        resumed = self.first - timedelta(minutes=10)  # five minutes after the old due time
        plan(self.ctx, resumed)
        due = [r for r in self.ctx.store.due_reminders(resumed) if r["event_id"] == self.master["id"]]
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["recurrence_id"], iso_utc(self.first))
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=resumed)
        self.assertEqual((stats["sent"], len(channel.sent)), (1, 1))

    def test_failed_occurrence_enumeration_retains_old_waiting_rows(self):
        old_id = rem.notice_id_for(self.master["id"], iso_utc(self.first), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(self.first), 15,
                                         iso_utc(self.first - timedelta(minutes=15)), old_id)
        def unavailable(_start, _end):
            raise RuntimeError("calendar read failed")
        with self.assertRaisesRegex(RuntimeError, "calendar read failed"):
            rem.plan(self.ctx.store, unavailable, now=self.now)
        self.assertEqual(len(waiting(self.ctx, self.now, self.master["id"])), 1)

    def test_malformed_recurrence_cannot_clear_waiting_reminders(self):
        old_id = rem.notice_id_for(self.master["id"], iso_utc(self.first), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(self.first), 15,
                                         iso_utc(self.first - timedelta(minutes=15)), old_id)
        self.ctx.store.update_event(self.master["id"], {"rrule": "FREQ=NOT_A_RULE"})
        warning = tools._plan_reminders(self.ctx)
        self.assertIn("очередь напоминаний не обновлена", warning)
        self.assertEqual(len(waiting(self.ctx, self.now, self.master["id"])), 1)

    def test_expansion_cap_cannot_authorize_partial_queue_reconciliation(self):
        old_id = rem.notice_id_for(self.master["id"], iso_utc(self.first), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(self.first), 15,
                                         iso_utc(self.first - timedelta(minutes=15)), old_id)
        self.ctx.store.update_event(self.master["id"], {"rrule": "FREQ=MINUTELY"})
        warning = tools._plan_reminders(self.ctx)
        self.assertIn("exceeded 1000 occurrences", warning)
        self.assertEqual(len(waiting(self.ctx, self.now, self.master["id"])), 1)

    def test_invalid_recurrence_does_not_starve_unrelated_due_notice(self):
        due_at = self.first - timedelta(minutes=15)
        self.ctx.store.update_event(self.master["id"], {"rrule": "FREQ=NOT_A_RULE"})
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(self.first), 15, iso_utc(due_at),
                                         rem.notice_id_for(self.master["id"], iso_utc(self.first), 15, iso_utc(self.first)),
                                         iso_utc(self.first))
        good = self.ctx.store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": "Valid",
                                            "start_utc": iso_utc(self.first),
                                            "end_utc": iso_utc(self.first + timedelta(hours=1)), "reminders_json": "[15]"})
        self.ctx.store.schedule_reminder(good["id"], good["start_utc"], 15, iso_utc(due_at),
                                         rem.notice_id_for(good["id"], good["start_utc"], 15))
        channel = Channel()
        stats = rem.deliver_due(self.ctx.store, channel, UTC, now=due_at)
        self.assertEqual((stats["sent"], stats["deferred"], stats["skipped"], len(channel.sent)), (1, 1, 0, 1))
        self.assertIn("Valid", channel.sent[0][1])
        self.assertEqual(len(self.ctx.store.due_reminders(due_at)), 1)

    def test_old_database_adds_original_slot_column_without_rewriting_uncertain_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "calendar.sqlite3")
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE reminders (id TEXT PRIMARY KEY, event_id TEXT NOT NULL,"
                             " occurrence_start_utc TEXT NOT NULL, offset_min INTEGER NOT NULL, fire_at_utc TEXT NOT NULL,"
                             " notice_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'scheduled',"
                             " sent_at TEXT, detail TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,"
                             " updated_at TEXT NOT NULL)")
                conn.execute("INSERT INTO reminders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             ("old", "series", iso_utc(self.first), 15, iso_utc(self.first - timedelta(minutes=15)),
                              "cal:old", "unknown", None, "host outcome unconfirmed", 1, iso_utc(self.now)))
            reopened = Store(directory)
            with reopened._conn() as conn:
                row = dict(conn.execute("SELECT * FROM reminders WHERE id='old'").fetchone())
            self.assertEqual((row["recurrence_id"], row["state"], row["notice_id"]), ("", "unknown", "cal:old"))
            self.assertEqual(reopened.unknown_reminder_count(), 1)

    def test_following_split_keeps_a_sent_occurrence_terminal_under_new_series_id(self):
        start = self.first
        plan(self.ctx, self.now)
        channel = Channel()
        sent = rem.deliver_due(self.ctx.store, channel, UTC, now=start - timedelta(minutes=15))
        self.assertEqual(sent["sent"], 1)
        updated = json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(start)}',
                                              title="Retitled", scope="following", confirm=True))
        self.assertEqual(updated["status"], "updated")
        plan(self.ctx, start - timedelta(minutes=10))
        later = rem.deliver_due(self.ctx.store, channel, UTC, now=start - timedelta(minutes=10))
        self.assertEqual((later["sent"], len(channel.sent)), (0, 1))

    def test_following_real_move_after_sent_schedules_new_time_once(self):
        plan(self.ctx, self.now)
        channel = Channel()
        sent = rem.deliver_due(self.ctx.store, channel, UTC, now=self.first - timedelta(minutes=15))
        self.assertEqual(sent["sent"], 1)
        moved = self.first + timedelta(hours=2)
        updated = json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(self.first)}',
                                              start=iso_utc(moved), scope="following", confirm=True))
        self.assertEqual(updated["status"], "updated")
        plan(self.ctx, moved - timedelta(minutes=20))
        fresh = rem.deliver_due(self.ctx.store, channel, UTC, now=moved - timedelta(minutes=15))
        self.assertEqual((fresh["sent"], len(channel.sent)), (1, 2))
        again = rem.deliver_due(self.ctx.store, channel, UTC, now=moved - timedelta(minutes=10))
        self.assertEqual((again["sent"], len(channel.sent)), (0, 2))

    def test_following_real_move_after_unknown_uses_new_slot_not_old_retry(self):
        plan(self.ctx, self.now)
        old = next(r for r in self.ctx.store.upcoming_reminders(self.now, 200)
                   if r["event_id"] == self.master["id"] and r["occurrence_start_utc"] == iso_utc(self.first))
        self.ctx.store.mark_reminder(old["id"], "unknown")
        moved = self.first + timedelta(hours=2)
        json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(self.first)}',
                                    start=iso_utc(moved), scope="following", confirm=True))
        plan(self.ctx, moved - timedelta(minutes=20))
        channel = Channel()
        delivered = rem.deliver_due(self.ctx.store, channel, UTC, now=moved - timedelta(minutes=15))
        self.assertEqual((delivered["sent"], len(channel.sent)), (1, 1))
        self.assertEqual(self.ctx.store.unknown_reminder_count(), 1)

    def test_following_split_never_retries_an_uncertain_prior_send(self):
        plan(self.ctx, self.now)
        row = next(r for r in self.ctx.store.upcoming_reminders(self.now, 200)
                   if r["event_id"] == self.master["id"] and r["occurrence_start_utc"] == iso_utc(self.first))
        self.ctx.store.mark_reminder(row["id"], "unknown")
        json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(self.first)}',
                                    title="Retitled", scope="following", confirm=True))
        plan(self.ctx, self.first - timedelta(minutes=10))
        channel = Channel()
        later = rem.deliver_due(self.ctx.store, channel, UTC, now=self.first - timedelta(minutes=10))
        self.assertEqual((later["sent"], channel.sent), (0, []))

    def test_following_shift_keeps_sent_moved_exception_at_its_explicit_time(self):
        effective = self.second + timedelta(hours=2)
        self.move(self.second, effective, "Exception keeps this time")
        planning_time = effective - timedelta(hours=1)
        plan(self.ctx, planning_time)
        channel = Channel()
        first_send = rem.deliver_due(self.ctx.store, channel, UTC, now=effective - timedelta(minutes=15))
        self.assertEqual(first_send["sent"], 1)
        self.assertIn("Exception keeps this time", channel.sent[0][1])
        updated = json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(self.first)}',
                                              start=iso_utc(self.first + timedelta(hours=1)), scope="following", confirm=True))
        self.assertEqual(updated["status"], "updated")
        plan(self.ctx, effective - timedelta(minutes=10))
        second_send = rem.deliver_due(self.ctx.store, channel, UTC, now=effective - timedelta(minutes=10))
        self.assertEqual((second_send["sent"], len(channel.sent)), (0, 1))

    def test_following_shift_keeps_legacy_sent_moved_exception_terminal(self):
        effective = self.second + timedelta(hours=2)
        self.move(self.second, effective, "Legacy moved")
        legacy_id = rem.notice_id_for(self.master["id"], iso_utc(effective), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(effective), 15,
                                         iso_utc(effective - timedelta(minutes=15)), legacy_id)
        row = next(r for r in self.ctx.store.upcoming_reminders(self.now, 200) if r["notice_id"] == legacy_id)
        self.ctx.store.mark_reminder(row["id"], "sent")
        json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(self.first)}',
                                    start=iso_utc(self.first + timedelta(hours=1)), scope="following", confirm=True))
        plan(self.ctx, effective - timedelta(minutes=10))
        channel = Channel()
        later = rem.deliver_due(self.ctx.store, channel, UTC, now=effective - timedelta(minutes=10))
        self.assertEqual((later["sent"], channel.sent), (0, []))

    def test_title_only_following_split_preserves_moved_cut_occurrence_time(self):
        effective = self.first + timedelta(hours=2)
        exception = self.move(self.first, effective, "Moved cut")
        updated = json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(self.first)}',
                                              title="Renamed", scope="following", confirm=True))
        self.assertEqual(updated["status"], "updated")
        saved = self.ctx.store.get_event(exception["id"])
        self.assertEqual((saved["start_utc"], saved["end_utc"]),
                         (iso_utc(effective), iso_utc(effective + timedelta(hours=1))))
        self.assertEqual(self.ctx.store.get_event(saved["master_id"])["start_utc"], iso_utc(self.first))

    def test_title_only_following_split_does_not_repeat_sent_moved_cut(self):
        effective = self.first + timedelta(hours=2)
        self.move(self.first, effective, "Moved cut")
        plan(self.ctx, self.now)
        channel = Channel()
        first_send = rem.deliver_due(self.ctx.store, channel, UTC, now=effective - timedelta(minutes=15))
        self.assertEqual(first_send["sent"], 1)
        json.loads(tools.cal_update(self.ctx, id=f'{self.master["id"]}@{iso_utc(self.first)}',
                                    title="Retitled", scope="following", confirm=True))
        plan(self.ctx, effective - timedelta(minutes=10))
        second_send = rem.deliver_due(self.ctx.store, channel, UTC, now=effective - timedelta(minutes=10))
        self.assertEqual((second_send["sent"], len(channel.sent)), (0, 1))

    def test_rename_following_moved_cut_preserves_later_slots_and_renames_cut(self):
        effective = self.first + timedelta(hours=2)
        self.move(self.first, effective, "Old cut title")
        result = ops.update_event(self.ctx.store, self.ctx.providers,
                                  f'{self.master["id"]}@{iso_utc(self.first)}',
                                  {"title": "New title"}, scope="following")
        self.assertEqual(result["status"], "ok")
        segment = self.ctx.store.get_event(result["split_master_id"])
        self.assertEqual(segment["start_utc"], iso_utc(self.first))
        cut = next(exc for exc in self.ctx.store.exceptions_for(segment["id"])
                   if exc["recurrence_id"] == iso_utc(self.first))
        self.assertEqual((cut["start_utc"], cut["title"]), (iso_utc(effective), "New title"))
        later = [occ for occ in self.ctx.occurrences(self.second - timedelta(minutes=1),
                                                      self.second + timedelta(hours=1))
                 if occ.get("master_id") == segment["id"] or occ.get("id", "").startswith(segment["id"])]
        self.assertTrue(any(occ["start_utc"] == iso_utc(self.second) for occ in later))

    def test_split_carries_legacy_sent_moved_before_cut_from_later_original_slot(self):
        # The later original slot was moved before the split boundary; old queue rows have no recurrence_id.
        effective = self.first + timedelta(hours=1)
        self.move(self.second, effective, "Earlier moved later slot")
        legacy = rem.notice_id_for(self.master["id"], iso_utc(effective), 15)
        self.ctx.store.schedule_reminder(self.master["id"], iso_utc(effective), 15,
                                         iso_utc(effective - timedelta(minutes=15)), legacy)
        row = next(r for r in self.ctx.store.upcoming_reminders(self.now, 200) if r["notice_id"] == legacy)
        self.ctx.store.mark_reminder(row["id"], "sent")
        result = ops.update_event(self.ctx.store, self.ctx.providers,
                                  f'{self.master["id"]}@{iso_utc(self.second)}',
                                  {"title": "Retitled"}, scope="following")
        self.assertEqual(result["status"], "ok")
        segment = self.ctx.store.get_event(result["split_master_id"])
        with self.ctx.store._conn() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM reminders WHERE event_id=?", (segment["id"],))]
        self.assertTrue(any(r["state"] == "sent" and r["recurrence_id"] == iso_utc(self.second) for r in rows))
        plan(self.ctx, effective - timedelta(minutes=10))
        channel = Channel()
        rem.deliver_due(self.ctx.store, channel, UTC, now=effective - timedelta(minutes=10))
        self.assertFalse(any("Earlier moved later slot" in text for _, text in channel.sent))
        self.assertFalse(any(r["event_id"] == segment["id"] for r in self.ctx.store.due_reminders(effective)))


class CatchupBatchReservationTests(unittest.TestCase):
    def test_replanned_member_does_not_hold_back_its_batch_peers(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        events = []
        for name in ("Peer A", "Replanned", "Peer B"):
            event = ctx.store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": name, "reminders_json": "[15]",
                                            "start_utc": iso_utc(now + timedelta(minutes=10)), "end_utc": iso_utc(now + timedelta(hours=1))})
            ctx.store.schedule_reminder(event["id"], event["start_utc"], 15, iso_utc(now - timedelta(minutes=20)),
                                        rem.notice_id_for(event["id"], event["start_utc"], 15))
            events.append(event)
        victim = events[1]
        original = ctx.store.reserve_reminder_send

        def concurrent_replan(ids):
            # a plugin-side edit replaces the victim's waiting row between the live recheck and the reservation
            if len(ids) > 1:
                ctx.store.drop_reminders_for(victim["id"])
                ctx.store.schedule_reminder(victim["id"], victim["start_utc"], 15, iso_utc(now - timedelta(minutes=20)),
                                            rem.notice_id_for(victim["id"], victim["start_utc"], 15))
            return original(ids)

        channel = Channel()
        with patch.object(ctx.store, "reserve_reminder_send", side_effect=concurrent_replan):
            stats = rem.deliver_due(ctx.store, channel, UTC, now=now)
        self.assertEqual((stats["sent"], len(channel.sent)), (2, 1))
        self.assertIn("Peer A", channel.sent[0][1])
        self.assertIn("Peer B", channel.sent[0][1])
        self.assertNotIn("Replanned", channel.sent[0][1])
        self.assertEqual(channel.sent[0][0], rem.batch_notice_id([rem.notice_id_for(e["id"], e["start_utc"], 15) for e in (events[0], events[2])]))

        rem.deliver_due(ctx.store, channel, UTC, now=now + timedelta(minutes=1))
        self.assertEqual(len(channel.sent), 2)
        self.assertEqual(sum("Replanned" in text for _, text in channel.sent), 1)
        self.assertEqual(ctx.store.due_reminders(now + timedelta(minutes=1)), [])


if __name__ == "__main__":
    unittest.main()
