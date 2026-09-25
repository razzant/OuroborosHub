"""Adversarial QA scenarios for the calendar review snapshot.

These tests are intentionally written against the product contract. A failure
is a reproducible review finding, not an expected-failure annotation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

import ops  # noqa: E402
import reminders as rem  # noqa: E402
import tools  # noqa: E402
from model import (  # noqa: E402
    BUSY_COPY_TITLE,
    DEFAULT_LOCAL_CALENDAR_ID,
    get_tz,
    iso_utc,
    now_utc,
    parse_input,
    parse_stored,
)
from providers import ics_to_rows, row_to_ics  # noqa: E402
from providers_google import gevent_to_row, row_to_gevent  # noqa: E402
from store import Store  # noqa: E402


class FakeAPI:
    def __init__(self, secrets=None):
        self.state_dir = tempfile.mkdtemp(prefix="calendar-review-")
        self.secrets = secrets or {}

    def get_state_dir(self):
        return self.state_dir

    def get_settings(self, keys):
        return {key: self.secrets.get(key, "") for key in keys}

    def get_runtime_info(self):
        return {"server_port": 8765}


class RecordingAdapter:
    def __init__(self):
        self.calls = []
        self.remote_by_uid = {}
        self.fail_after_accept_once = False
        self.fail_calendar = ""

    def create(self, calendar, event, payload):
        self.calls.append(("create", calendar["id"], event["uid"], event["title"]))
        remote = self.remote_by_uid.setdefault(
            event["uid"],
            {"external_id": "remote-" + event["uid"][:12], "href": "https://example.test/" + event["uid"], "etag": '"e1"'},
        )
        if self.fail_after_accept_once:
            self.fail_after_accept_once = False
            raise ops.ProviderError("network", "response was lost after provider acceptance")
        if self.fail_calendar and calendar["id"] == self.fail_calendar:
            raise ops.ProviderError("network", "one calendar is offline")
        return dict(remote)

    def update(self, calendar, event, expected_etag, payload):
        self.calls.append(("update", calendar["id"], event["id"], payload, event.get("rrule"), event.get("status")))
        return {"etag": '"e2"'}

    def delete(self, calendar, event, expected_etag):
        self.calls.append(("delete", calendar["id"], event["id"]))

    def respond(self, calendar, event, payload):
        self.calls.append(("respond", calendar["id"], event["id"], payload))
        return {"etag": '"r2"'}


class Providers:
    def __init__(self, adapter):
        self.adapter = adapter

    def adapter_for(self, account_id):
        return self.adapter


def make_context():
    return tools.Context(FakeAPI())


def add_external_calendar(store, suffix, *, publish=True, publish_mode="busy"):
    account = "yandex:review@example.test"
    store.upsert_account(
        {"id": account, "provider": "yandex", "alias": "review", "login": "review@example.test", "status": "ok"}
    )
    calendar_id = f"{account}:{suffix}"
    store.upsert_calendar(
        {
            "id": calendar_id,
            "account_id": account,
            "provider": "yandex",
            "external_id": suffix,
            "href": f"https://caldav.yandex.ru/calendars/review/{suffix}/",
            "name": suffix,
            "writable": True,
            "role_publish": publish,
            "publish_mode": publish_mode,
        }
    )
    return calendar_id


class RecurrenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.ctx.tz = get_tz("Asia/Dubai")

    def create_series(self, **overrides):
        args = {
            "title": "Daily",
            "start": "2026-09-28T09:00",
            "duration_min": 60,
            "rrule": "FREQ=DAILY",
            "confirm": True,
        }
        args.update(overrides)
        result = json.loads(tools.cal_create(self.ctx, **args))
        return result["event"]

    def test_moved_occurrence_appears_in_destination_window(self):
        master = self.create_series()
        first_window = json.loads(
            tools.cal_events(self.ctx, start="2026-09-29", end="2026-09-30")
        )["events"]
        occurrence = first_window[0]
        json.loads(
            tools.cal_update(
                self.ctx,
                id=occurrence["id"],
                start="2026-10-10T11:00",
                scope="this",
                confirm=True,
            )
        )
        destination = json.loads(
            tools.cal_events(self.ctx, start="2026-10-10", end="2026-10-11")
        )["events"]
        starts = [event["start"][11:16] for event in destination]
        self.assertIn("11:00", starts)
        self.assertTrue(any(event.get("series_id") == master["id"] for event in destination))

    def test_following_split_preserves_later_exception(self):
        self.create_series()
        week = json.loads(
            tools.cal_events(self.ctx, start="2026-09-28", end="2026-10-05")
        )["events"]
        later = next(event for event in week if event["start"][:10] == "2026-10-02")
        json.loads(
            tools.cal_update(
                self.ctx,
                id=later["id"],
                start="2026-10-02T11:00",
                scope="this",
                confirm=True,
            )
        )
        split_at = next(event for event in week if event["start"][:10] == "2026-09-30")
        json.loads(
            tools.cal_update(
                self.ctx,
                id=split_at["id"],
                start="2026-09-30T10:00",
                scope="following",
                confirm=True,
            )
        )
        friday = json.loads(
            tools.cal_events(self.ctx, start="2026-10-02", end="2026-10-03")
        )["events"]
        self.assertEqual([event["start"][11:16] for event in friday], ["11:00"])

    def test_following_split_gets_a_new_link_group(self):
        external = add_external_calendar(self.ctx.store, "work")
        self.ctx.providers = Providers(RecordingAdapter())
        result = json.loads(
            tools.cal_create(
                self.ctx,
                title="Linked daily",
                start="2026-09-28T09:00",
                duration_min=60,
                rrule="FREQ=DAILY",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, external],
                confirm=True,
            )
        )
        old_group = result["event"]["link_group_id"]
        occurrence = json.loads(
            tools.cal_events(self.ctx, start="2026-09-30", end="2026-10-01")
        )["events"][0]
        json.loads(
            tools.cal_update(
                self.ctx,
                id=occurrence["id"],
                start="2026-09-30T10:00",
                scope="following",
                confirm=True,
            )
        )
        future = json.loads(
            tools.cal_events(self.ctx, start="2026-10-02", end="2026-10-03")
        )["events"]
        future_groups = {event.get("link_group_id") for event in future}
        self.assertEqual(len(future_groups), 1)
        self.assertNotIn(old_group, future_groups)

    def test_timed_recurrence_keeps_wall_clock_across_dst(self):
        self.ctx.tz = get_tz("America/New_York")
        json.loads(
            tools.cal_create(
                self.ctx,
                title="DST timed",
                start="2026-03-01T09:00",
                duration_min=60,
                rrule="FREQ=WEEKLY",
                confirm=True,
            )
        )
        after_dst = json.loads(
            tools.cal_events(self.ctx, start="2026-03-08", end="2026-03-09")
        )["events"]
        self.assertEqual((after_dst[0]["start"][11:16], after_dst[0]["end"][11:16]), ("09:00", "10:00"))

    def test_all_day_recurrence_remains_whole_day_across_dst(self):
        self.ctx.tz = get_tz("America/New_York")
        json.loads(
            tools.cal_create(
                self.ctx,
                title="DST all day",
                start="2026-03-08",
                end="2026-03-09",
                all_day=True,
                rrule="FREQ=WEEKLY",
                confirm=True,
            )
        )
        after_dst = json.loads(
            tools.cal_events(self.ctx, start="2026-03-15", end="2026-03-16")
        )["events"]
        self.assertEqual(after_dst[0]["start"][:10], "2026-03-15")
        self.assertEqual(after_dst[0]["end"], "2026-03-16T00:00:00-04:00")


class LinkedCopyAndIntentReviewTests(unittest.TestCase):
    def test_three_calendars_report_one_partial_failure(self):
        ctx = make_context()
        first = add_external_calendar(ctx.store, "first")
        second = add_external_calendar(ctx.store, "second")
        adapter = RecordingAdapter()
        adapter.fail_calendar = first
        ctx.providers = Providers(adapter)
        result = json.loads(
            tools.cal_create(
                ctx,
                title="Partial",
                start="2026-09-28T12:00+00:00",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, first, second],
                confirm=True,
            )
        )
        statuses = {item["calendar_id"]: item["status"] for item in result["assignments"]}
        self.assertEqual(
            statuses,
            {DEFAULT_LOCAL_CALENDAR_ID: "done", first: "pending", second: "done"},
        )
        self.assertEqual(len(ctx.store.group_members(result["event"]["link_group_id"])), 3)

    def test_reassign_creates_busy_copy_with_same_uid(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store, "work", publish_mode="busy")
        adapter = RecordingAdapter()
        ctx.providers = Providers(adapter)
        created = json.loads(
            tools.cal_create(
                ctx,
                title="Private appointment",
                start="2026-09-28T12:00+00:00",
                location="Secret place",
                confirm=True,
            )
        )
        source = ctx.store.get_event(created["event"]["id"])
        json.loads(
            tools.cal_update(
                ctx,
                id=source["id"],
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, external],
                confirm=True,
            )
        )
        copy = next(row for row in ctx.store.group_members(ctx.store.get_event(source["id"])["link_group_id"]) if row["calendar_id"] == external)
        self.assertEqual(copy["title"], BUSY_COPY_TITLE)
        self.assertEqual(copy["location"], "")
        self.assertEqual(copy["uid"], source["uid"])
        self.assertEqual(adapter.calls[0][2], source["uid"])

    def test_retry_after_lost_response_reuses_provider_identity(self):
        store = Store(tempfile.mkdtemp(prefix="calendar-review-"))
        external = add_external_calendar(store, "retry")
        adapter = RecordingAdapter()
        adapter.fail_after_accept_once = True
        providers = Providers(adapter)
        event = store.insert_event(
            {
                "calendar_id": external,
                "uid": "stable-uid@example.test",
                "title": "Idempotent",
                "start_utc": "2026-09-28T12:00:00+00:00",
                "end_utc": "2026-09-28T13:00:00+00:00",
                "sync_state": "pending",
            }
        )
        intent = store.add_intent("create", "yandex:review@example.test", external, event["id"], {})
        first = ops.execute_intent(store, providers, intent, "child:1")
        second = ops.execute_intent(store, providers, intent, "companion")
        third = ops.execute_intent(store, providers, intent, "child:2")
        self.assertEqual((first["status"], second["status"], third["status"]), ("pending", "done", "pending"))
        self.assertEqual(len(adapter.remote_by_uid), 1)
        self.assertEqual(store.get_event(event["id"])["external_id"], "remote-stable-uid@e")

    def test_two_threads_cannot_hold_same_lease(self):
        store = Store(tempfile.mkdtemp(prefix="calendar-review-"))
        event = store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Lease",
                "start_utc": "2026-09-28T12:00:00+00:00",
                "end_utc": "2026-09-28T13:00:00+00:00",
            }
        )
        intent = store.add_intent("create", "yandex:x", "yandex:x:c", event["id"], {})
        barrier = threading.Barrier(3)
        winners = []

        def claim(owner):
            barrier.wait()
            winners.append(store.lease_intent(intent["id"], owner))

        threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("writer-a", "writer-b")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(item is not None for item in winners), 1)

    def test_delete_following_updates_provider_with_truncated_master(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store, "series", publish_mode="full")
        adapter = RecordingAdapter()
        ctx.providers = Providers(adapter)
        json.loads(
            tools.cal_create(
                ctx,
                title="External series",
                start="2026-09-28T12:00+00:00",
                rrule="FREQ=DAILY",
                calendars=[external],
                confirm=True,
            )
        )
        occurrence = json.loads(
            tools.cal_events(ctx, start="2026-09-30", end="2026-10-01")
        )["events"][0]
        result = json.loads(
            tools.cal_delete(ctx, id=occurrence["id"], scope="following", confirm=True)
        )
        self.assertEqual(result["status"], "deleted")
        update = adapter.calls[-1]
        self.assertEqual((update[0], update[5]), ("update", "confirmed"))
        self.assertIn("UNTIL=", update[4])


class ReminderReviewTests(unittest.TestCase):
    class Channel:
        def __init__(self, result=("sent", "accepted")):
            self.result = result
            self.sent = []

        def send(self, notice_id, text):
            self.sent.append((notice_id, text))
            return self.result

    def test_cancelled_or_moved_recurring_occurrence_is_reread(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        created = json.loads(
            tools.cal_create(
                ctx,
                title="Recurring reminder",
                start=(now + timedelta(minutes=20)).isoformat(),
                duration_min=30,
                reminders=[15],
                rrule="FREQ=DAILY",
                confirm=True,
            )
        )
        occurrence = json.loads(
            tools.cal_events(
                ctx,
                start=(now + timedelta(minutes=1)).isoformat(),
                end=(now + timedelta(hours=1)).isoformat(),
            )
        )["events"][0]
        self.assertEqual(occurrence["series_id"], created["event"]["id"])
        json.loads(
            tools.cal_update(
                ctx,
                id=occurrence["id"],
                start=(now + timedelta(hours=2)).isoformat(),
                scope="this",
                confirm=True,
            )
        )
        channel = self.Channel()
        stats = rem.deliver_due(ctx.store, channel, ctx.tz, now=now + timedelta(minutes=10))
        self.assertEqual(channel.sent, [])
        self.assertEqual(stats["skipped"], 1)

    def test_no_channel_notice_is_delivered_when_channel_recovers(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        event = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Channel recovery",
                "start_utc": iso_utc(now + timedelta(minutes=20)),
                "end_utc": iso_utc(now + timedelta(minutes=50)),
                "reminders_json": "[30]",
            }
        )
        rem.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=now)
        unavailable = self.Channel(("no_route", "no route"))
        rem.deliver_due(ctx.store, unavailable, ctx.tz, now=now)
        recovered = self.Channel()
        rem.deliver_due(ctx.store, recovered, ctx.tz, now=now + timedelta(minutes=1))
        self.assertEqual(len(recovered.sent), 1)
        self.assertEqual(ctx.store.upcoming_reminders(now)[0]["event_id"], event["id"])

    def test_catchup_skips_finished_recurring_occurrence(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        event = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Finished recurrence",
                "start_utc": iso_utc(now - timedelta(hours=2)),
                "end_utc": iso_utc(now - timedelta(hours=1)),
                "rrule": "FREQ=DAILY",
            }
        )
        ctx.store.schedule_reminder(
            event["id"],
            iso_utc(now - timedelta(hours=2)),
            15,
            iso_utc(now - timedelta(hours=2, minutes=15)),
            "cal:finished",
        )
        channel = self.Channel()
        stats = rem.deliver_due(ctx.store, channel, ctx.tz, now=now)
        self.assertEqual(channel.sent, [])
        self.assertEqual(stats["skipped"], 1)

    def test_delivery_retries_are_bounded(self):
        ctx = make_context()
        now = now_utc().replace(microsecond=0)
        event = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Retry bound",
                "start_utc": iso_utc(now + timedelta(minutes=10)),
                "end_utc": iso_utc(now + timedelta(hours=1)),
            }
        )
        ctx.store.schedule_reminder(event["id"], event["start_utc"], 15, iso_utc(now - timedelta(minutes=5)), "cal:retry-bound")
        channel = self.Channel(("retry", "HTTP 503"))
        for _ in range(6):
            rem.deliver_due(ctx.store, channel, ctx.tz, now=now)
        self.assertEqual(ctx.store.due_reminders(now), [])

    def test_batch_notice_id_never_exceeds_host_limit(self):
        notice_ids = ["cal:" + ("x" * 120) + str(index) for index in range(200)]
        self.assertLessEqual(len(rem.batch_notice_id(notice_ids)), 128)


class ProviderSerializationReviewTests(unittest.TestCase):
    def test_yandex_new_event_serializes_attendees(self):
        event = {
            "uid": "new-meeting@example.test",
            "title": "New meeting",
            "start_utc": "2026-09-28T09:00:00+00:00",
            "end_utc": "2026-09-28T10:00:00+00:00",
            "tz": "UTC",
            "all_day": 0,
            "attendees_json": '[{"email":"alice@example.test","name":"Alice"}]',
            "organizer": "owner@example.test",
            "reminders_json": "[]",
        }
        out = row_to_ics(event)
        self.assertIn("ATTENDEE", out)
        self.assertIn("mailto:alice@example.test", out)

    def test_yandex_serialization_keeps_attendees_and_increments_sequence(self):
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
            "UID:meeting-1\r\nSEQUENCE:5\r\nSUMMARY:Meeting\r\n"
            "DTSTART:20260928T090000Z\r\nDTEND:20260928T100000Z\r\n"
            "ATTENDEE;CN=Alice:mailto:alice@example.test\r\n"
            "X-YANDEX-COLOR:#abcdef\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        row = ics_to_rows(ics, "calendar", "href", '"e1"', get_tz("UTC"))[0]
        out = row_to_ics({**row, "title": "Meeting changed"})
        self.assertIn("ATTENDEE;CN=Alice:mailto:alice@example.test", out)
        self.assertIn("X-YANDEX-COLOR:#abcdef", out)
        self.assertIn("SEQUENCE:6", out)

    def test_google_all_day_roundtrip_preserves_exclusive_end_date(self):
        item = {
            "id": "all-day",
            "iCalUID": "all-day@example.test",
            "start": {"date": "2026-03-08"},
            "end": {"date": "2026-03-09"},
            "status": "confirmed",
        }
        row = gevent_to_row(item, "google:test:primary", get_tz("America/New_York"))
        body = row_to_gevent(row)
        self.assertEqual(body["start"], {"date": "2026-03-08"})
        self.assertEqual(body["end"], {"date": "2026-03-09"})

    def test_rsvp_uses_provider_respond_operation(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store, "rsvp", publish_mode="full")
        adapter = RecordingAdapter()
        ctx.providers = Providers(adapter)
        event = ctx.store.insert_event(
            {
                "calendar_id": external,
                "uid": "invite@example.test",
                "external_id": "provider-event",
                "etag": '"e1"',
                "title": "Invite",
                "start_utc": "2026-09-28T12:00:00+00:00",
                "end_utc": "2026-09-28T13:00:00+00:00",
                "attendees_json": '[{"email":"review@example.test","self":true,"status":"needsAction"}]',
                "origin": "external",
            }
        )
        result = json.loads(
            tools.cal_update(ctx, id=event["id"], response="accepted", confirm=True)
        )
        self.assertEqual(result["status"], "updated")
        self.assertEqual(adapter.calls[0][0], "respond")


class ContractReviewTests(unittest.TestCase):
    def test_all_resolves_to_default_plus_publish_set(self):
        ctx = make_context()
        published = add_external_calendar(ctx.store, "published", publish=True)
        add_external_calendar(ctx.store, "not-published", publish=False)
        ids, error = ctx.resolve_calendars("all")
        self.assertEqual(error, "")
        self.assertEqual(ids, [DEFAULT_LOCAL_CALENDAR_ID, published])

    def test_busy_set_matches_documented_default_plus_publish_set(self):
        ctx = make_context()
        published = add_external_calendar(ctx.store, "published", publish=True)
        ids, error = ctx.resolve_calendars("busy_set")
        self.assertEqual(error, "")
        self.assertEqual(ids, [DEFAULT_LOCAL_CALENDAR_ID, published])

    def test_mutating_tools_refuse_without_confirm(self):
        ctx = make_context()
        before = ctx.store.counts()
        create = json.loads(tools.cal_create(ctx, title="No", start="2026-09-28T12:00"))
        settings = json.loads(tools.cal_settings(ctx, action="new_local_calendar", name="No"))
        reminder = json.loads(tools.cal_reminders(ctx, action="set_default", offsets=[15]))
        self.assertEqual((create["status"], settings["status"], reminder["status"]), ("needs_confirm", "needs_confirm", "needs_confirm"))
        self.assertEqual(ctx.store.counts(), before)
        self.assertIsNone(ctx.store.get_setting("reminder_defaults"))
        seeded = json.loads(tools.cal_create(ctx, title="Keep", start="2026-09-28T12:00", confirm=True))["event"]
        update = json.loads(tools.cal_update(ctx, id=seeded["id"], title="Changed"))
        delete = json.loads(tools.cal_delete(ctx, id=seeded["id"]))
        self.assertEqual((update["status"], delete["status"]), ("needs_confirm", "needs_confirm"))
        self.assertEqual(ctx.store.get_event(seeded["id"])["title"], "Keep")
        self.assertIsNone(ctx.store.get_event(seeded["id"])["deleted_at"])

    def test_changed_reminder_rules_replace_old_schedule(self):
        ctx = make_context()
        json.loads(tools.cal_reminders(ctx, action="set_default", offsets=[15], confirm=True))
        start = iso_utc(now_utc() + timedelta(hours=4))
        json.loads(tools.cal_create(ctx, title="Replan", start=start, confirm=True))
        before = rem.upcoming(ctx.store, ctx.tz, limit=10)
        self.assertEqual({item["offset_min"] for item in before}, {15})
        json.loads(tools.cal_reminders(ctx, action="set_default", offsets=[30], confirm=True))
        after = rem.upcoming(ctx.store, ctx.tz, limit=10)
        self.assertEqual({item["offset_min"] for item in after}, {30})

    def test_needs_confirm_response_respects_15000_character_limit(self):
        ctx = make_context()
        result = tools.cal_create(
            ctx,
            title="x" * 20_000,
            start="2026-09-28T12:00",
            confirm=False,
        )
        self.assertLessEqual(len(result), 15_000)
        self.assertEqual(json.loads(result)["status"], "needs_confirm")

    def test_missing_secrets_degrades_honestly(self):
        ctx = make_context()
        status = json.loads(tools.cal_status(ctx))
        self.assertEqual(status["status"], "ok")
        self.assertTrue(any("Яндекс не подключён" in step for step in status["next_step"]))
        self.assertTrue(any("Google не подключён" in step for step in status["next_step"]))


class MigrationReviewTests(unittest.TestCase):
    def test_prototype_migration_is_idempotent_on_reopen(self):
        state_dir = tempfile.mkdtemp(prefix="calendar-review-migration-")
        db = os.path.join(state_dir, "calendar.sqlite3")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE events (id TEXT PRIMARY KEY, calendar TEXT NOT NULL, title TEXT NOT NULL, start TEXT NOT NULL, "
                "end TEXT NOT NULL, all_day INTEGER NOT NULL DEFAULT 0, location TEXT, description TEXT, remind_min INTEGER NOT NULL DEFAULT 0, "
                "created TEXT NOT NULL, updated TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'local', calendar_name TEXT NOT NULL DEFAULT 'Личное', "
                "uid TEXT, href TEXT, etag TEXT, recurring INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE TABLE calendars (id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, href TEXT, writable INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO events (id, calendar, title, start, end, created, updated, source) "
                "VALUES ('legacy','local:personal','Legacy','2026-09-22T09:00:00+04:00','2026-09-22T10:00:00+04:00','x','x','local')"
            )
        first = Store(state_dir)
        second = Store(state_dir)
        self.assertIsNotNone(first.get_event("legacy"))
        self.assertIsNotNone(second.get_event("legacy"))
        with second._conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE id='legacy'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
