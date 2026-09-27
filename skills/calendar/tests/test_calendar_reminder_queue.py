"""Reminder queue consistency: explicit «no reminders» per event, provider-side offset/exception changes
reaching the queue, and a replanned member not holding back a catch-up batch.

All persistence lives in temporary SQLite directories; provider and Host Service traffic are in-memory fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
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
import routes  # noqa: E402
import tools  # noqa: E402
import worker as calendar_worker  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, iso_utc, now_utc  # noqa: E402
from providers import row_to_ics  # noqa: E402
from providers_google import GoogleAdapter, gevent_to_row, row_to_gevent  # noqa: E402

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
