"""Round 3 adversarial QA for calendar 1.0.4 (snapshot 181170e).

Every test uses an isolated temporary SQLite state directory.  Failures are
reproducible review findings, not expected-failure annotations.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
for path in (SKILL, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import (  # noqa: E402  (also installs the Starlette stub)
    Channel,
    FakeAPI,
    OneAdapterProviders,
    RecordingAdapter,
    Request,
    RouteAPI,
    add_external_calendar,
    add_local_calendar,
    make_context,
)

import ops  # noqa: E402
import reminders as rem  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc  # noqa: E402
from providers import YandexAdapter, ics_to_rows  # noqa: E402
from providers_google import GoogleAdapter, gevent_to_row, row_to_gevent, save_tokens  # noqa: E402


UTC = timezone.utc


def count_live_group_rows(store, group_id):
    with store._conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE link_group_id=? AND deleted_at IS NULL",
            (group_id,),
        ).fetchone()[0]


def make_series(ctx, calendars):
    ctx.tz = UTC
    return json.loads(
        tools.cal_create(
            ctx,
            title="Linked daily",
            start="2026-10-01T09:00+00:00",
            duration_min=60,
            rrule="FREQ=DAILY",
            calendars=calendars,
            confirm=True,
        )
    )["event"]


class ReassignAndRepeatedOccurrenceTests(unittest.TestCase):
    def test_reassign_copies_exceptions_then_removes_only_one_of_three_copies(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "second", "full")
        third = add_local_calendar(ctx.store, "third", "full")
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second, third])
        occurrence_key = "2026-10-04T09:00:00+00:00"
        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@{occurrence_key}",
                start="2026-10-04T12:00+00:00",
                scope="this",
                confirm=True,
            )
        )

        result = ops.reassign_event(
            ctx.store,
            ctx.providers,
            ctx.store.get_event(master["id"]),
            [DEFAULT_LOCAL_CALENDAR_ID, second],
        )

        self.assertEqual(len(result["removed"]), 1)
        masters = ctx.store.group_masters(master["link_group_id"])
        self.assertEqual({row["calendar_id"] for row in masters}, {DEFAULT_LOCAL_CALENDAR_ID, second})
        self.assertEqual(sum(len(ctx.store.exceptions_for(row["id"])) for row in masters), 2)
        self.assertEqual(count_live_group_rows(ctx.store, master["link_group_id"]), 4)
        cards = json.loads(
            tools.cal_events(
                ctx,
                start="2026-10-04",
                end="2026-10-05",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, second],
            )
        )["events"]
        self.assertEqual(len(cards), 2)
        self.assertEqual({card["start"][11:16] for card in cards}, {"12:00"})

    def test_reassign_retries_master_then_still_writes_copied_exceptions(self):
        class FailFirstCreate(RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.failed = False

            def create(self, calendar, event, payload):
                self.calls.append(("create", calendar["id"], event["id"], dict(payload)))
                if not self.failed:
                    self.failed = True
                    raise ops.ProviderError("network", "master create deferred")
                return {
                    "external_id": "remote-" + event["id"],
                    "href": "https://example.test/" + event["id"] + ".ics",
                    "etag": '"e1"',
                }

        ctx = make_context()
        external = add_external_calendar(ctx.store, "reassign-retry", "full")
        adapter = FailFirstCreate()
        ctx.providers = OneAdapterProviders(adapter)
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID])
        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@2026-10-04T09:00:00+00:00",
                start="2026-10-04T12:00+00:00",
                scope="this",
                confirm=True,
            )
        )

        result = ops.reassign_event(
            ctx.store,
            ctx.providers,
            ctx.store.get_event(master["id"]),
            [DEFAULT_LOCAL_CALENDAR_ID, external],
        )
        self.assertEqual(result["added"][0]["status"], "pending")
        external_master = next(
            row
            for row in ctx.store.group_masters(ctx.store.get_event(master["id"])["link_group_id"])
            if row["calendar_id"] == external
        )
        external_exception = ctx.store.exceptions_for(external_master["id"])[0]
        for intent in ctx.store.open_intents():
            ctx.store.settle_intent(intent["id"], "pending", {}, retry_in_sec=0)
        ops.retry_due_intents(ctx.store, ctx.providers)

        self.assertIn(
            "update",
            {intent["kind"] for intent in ctx.store.intents_for_event(external_exception["id"])},
        )
        self.assertEqual(ctx.store.get_event(external_exception["id"])["sync_state"], "synced")
        self.assertEqual([call[0] for call in adapter.calls], ["create", "create", "update"])

    def test_repeated_edit_and_delete_reuse_linked_exception_rows(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "repeat", "full")
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        key = "2026-10-04T09:00:00+00:00"
        original_id = f"{master['id']}@{key}"

        json.loads(tools.cal_update(ctx, id=original_id, start="2026-10-04T11:00+00:00", scope="this", confirm=True))
        first_cards = json.loads(
            tools.cal_events(ctx, start="2026-10-04", end="2026-10-05")
        )["events"]
        primary_card = next(card for card in first_cards if card.get("is_primary"))
        rows_after_first = count_live_group_rows(ctx.store, master["link_group_id"])

        json.loads(tools.cal_update(ctx, id=primary_card["id"], start="2026-10-04T12:00+00:00", scope="this", confirm=True))
        rows_after_second = count_live_group_rows(ctx.store, master["link_group_id"])
        one_calendar = json.loads(
            tools.cal_events(
                ctx,
                start="2026-10-04",
                end="2026-10-05",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID],
            )
        )["events"]
        self.assertEqual((rows_after_first, rows_after_second, len(one_calendar)), (4, 4, 1))
        self.assertEqual(one_calendar[0]["start"][11:16], "12:00")

        json.loads(tools.cal_delete(ctx, id=one_calendar[0]["id"], scope="this", confirm=True))
        json.loads(tools.cal_delete(ctx, id=original_id, scope="this", confirm=True))
        self.assertEqual(count_live_group_rows(ctx.store, master["link_group_id"]), 4)
        self.assertEqual(
            json.loads(
                tools.cal_events(
                    ctx,
                    start="2026-10-04",
                    end="2026-10-05",
                    calendars=[DEFAULT_LOCAL_CALENDAR_ID],
                )
            )["events"],
            [],
        )


class RecurrenceScopeRound3Tests(unittest.TestCase):
    def test_scope_all_from_late_occurrence_shifts_every_copy_without_losing_early_dates(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "all-second", "full")
        third = add_local_calendar(ctx.store, "all-third", "full")
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second, third])

        result = json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@2026-10-10T09:00:00+00:00",
                start="2026-10-10T11:30+00:00",
                scope="all",
                confirm=True,
            )
        )
        self.assertEqual(result["status"], "updated")
        masters = ctx.store.group_masters(master["link_group_id"])
        self.assertEqual({row["start_utc"] for row in masters}, {"2026-10-01T11:30:00+00:00"})
        early = json.loads(tools.cal_events(ctx, start="2026-10-01", end="2026-10-02"))["events"]
        self.assertEqual(len(early), 3)
        self.assertEqual({card["start"][11:16] for card in early}, {"11:30"})

    def test_following_shifts_late_exdates_on_every_linked_copy(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "exdate-copy", "full")
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        exdates = "2026-10-02T09:00:00+00:00,2026-10-05T09:00:00+00:00"
        for linked_master in ctx.store.group_masters(master["link_group_id"]):
            ctx.store.update_event(linked_master["id"], {"exdates": exdates})

        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@2026-10-03T09:00:00+00:00",
                start="2026-10-03T11:00+00:00",
                scope="following",
                confirm=True,
            )
        )

        self.assertEqual(json.loads(tools.cal_events(ctx, start="2026-10-02", end="2026-10-03"))["events"], [])
        shifted = json.loads(tools.cal_events(ctx, start="2026-10-04", end="2026-10-05"))["events"]
        self.assertEqual(len(shifted), 2)
        self.assertEqual({card["start"][11:16] for card in shifted}, {"11:00"})
        # The 5 October exclusion belongs to the new 11:00 wall-time series.
        self.assertEqual(json.loads(tools.cal_events(ctx, start="2026-10-05", end="2026-10-06"))["events"], [])

    def test_following_creates_intents_for_moved_exceptions_after_new_masters(self):
        ctx = make_context()
        first = add_external_calendar(ctx.store, "split-first", "full")
        second = add_external_calendar(ctx.store, "split-second", "full")
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        master = make_series(ctx, [first, second])
        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@2026-10-06T09:00:00+00:00",
                start="2026-10-06T12:00+00:00",
                scope="this",
                confirm=True,
            )
        )
        adapter.calls.clear()

        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@2026-10-03T09:00:00+00:00",
                start="2026-10-03T10:00+00:00",
                scope="following",
                confirm=True,
            )
        )

        future = json.loads(tools.cal_events(ctx, start="2026-10-06", end="2026-10-07"))["events"]
        self.assertEqual(len(future), 2)
        new_group = future[0]["link_group_id"]
        new_masters = ctx.store.group_masters(new_group)
        self.assertEqual(len({row["uid"] for row in new_masters}), 1)
        moved_ids = {exc["id"] for row in new_masters for exc in ctx.store.exceptions_for(row["id"])}
        self.assertEqual(len(moved_ids), 2)
        for moved_id in moved_ids:
            self.assertIn("update", {intent["kind"] for intent in ctx.store.intents_for_event(moved_id)})
        self.assertEqual([call[0] for call in adapter.calls].count("create"), 2)


class ReadOnlyPreflightRound3Tests(unittest.TestCase):
    def make_read_only_event(self):
        ctx = make_context()
        calendar_id = add_external_calendar(ctx.store, "reader", "full")
        ctx.store.upsert_calendar(
            {
                **ctx.store.get_calendar(calendar_id),
                "writable": False,
            }
        )
        event = ctx.store.insert_event(
            {
                "calendar_id": calendar_id,
                "uid": "readonly@example.test",
                "external_id": "readonly-remote",
                "href": "https://example.test/readonly.ics",
                "etag": '"r1"',
                "title": "Read only",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "origin": "external",
            }
        )
        return ctx, ctx.store.get_event(event["id"])

    def test_update_and_delete_read_only_event_leave_row_untouched(self):
        ctx, event = self.make_read_only_event()
        before = dict(ctx.store.get_event(event["id"]))
        update = ops.update_event(ctx.store, ctx.providers, event["id"], {"title": "Changed"}, scope="all")
        delete = ops.delete_event(ctx.store, ctx.providers, event["id"], scope="all")
        after = ctx.store.get_event(event["id"])
        self.assertEqual((update["status"], delete["status"]), ("failed", "failed"))
        for key in ("title", "deleted_at", "sync_state", "link_group_id"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(ctx.store.intents_for_event(event["id"]), [])

    def test_reassign_read_only_event_is_preflighted_before_local_copy(self):
        ctx, event = self.make_read_only_event()
        before_counts = ctx.store.counts()
        before = dict(ctx.store.get_event(event["id"]))

        result = ops.reassign_event(
            ctx.store,
            ctx.providers,
            event,
            [event["calendar_id"], DEFAULT_LOCAL_CALENDAR_ID],
        )

        self.assertEqual(ctx.store.counts(), before_counts)
        self.assertEqual(ctx.store.get_event(event["id"])["link_group_id"], before["link_group_id"])
        self.assertTrue(result.get("failed") or any(item.get("status") == "failed" for item in result.get("added", [])))


class MixedRsvpAndDisconnectRound3Tests(unittest.TestCase):
    def test_mixed_update_keeps_rsvp_durable_when_first_provider_call_is_deferred(self):
        class FailFirstUpdate(RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.failed = False

            def update(self, calendar, event, expected_etag, payload):
                self.calls.append(("update", calendar["id"], event["id"], dict(payload)))
                if not self.failed:
                    self.failed = True
                    raise ops.ProviderError("network", "first update deferred")
                return {"etag": '"e2"'}

        ctx = make_context()
        external = add_external_calendar(ctx.store, "mixed", "full")
        adapter = FailFirstUpdate()
        ctx.providers = OneAdapterProviders(adapter)
        event = ctx.store.insert_event(
            {
                "calendar_id": external,
                "uid": "mixed@example.test",
                "external_id": "remote-mixed",
                "etag": '"e1"',
                "title": "Invite",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "attendees_json": '[{"email":"owner@example.test","self":true,"status":"needsAction"}]',
                "origin": "external",
            }
        )

        result = json.loads(
            tools.cal_update(
                ctx,
                id=event["id"],
                title="Invite changed",
                response="accepted",
                confirm=True,
            )
        )
        self.assertEqual(result["status"], "pending")
        for intent in ctx.store.open_intents():
            ctx.store.settle_intent(intent["id"], "pending", {}, retry_in_sec=0)
        ops.retry_due_intents(ctx.store, ctx.providers)

        self.assertEqual([intent["kind"] for intent in ctx.store.intents_for_event(event["id"])], ["update", "rsvp"])
        self.assertEqual([call[0] for call in adapter.calls], ["update", "update", "respond"])

    def test_disconnect_cancels_all_open_intents_drops_tokens_and_reload_cannot_resurrect(self):
        api = FakeAPI(
            {
                "GOOGLE_CALENDAR_CLIENT_ID": "client",
                "CALENDAR_TOKEN_KEY": "owner-passphrase",
            }
        )
        ctx = tools.Context(api)
        email = "owner@example.test"
        account_id = "google:" + email
        save_tokens(
            ctx.state_dir,
            "owner-passphrase",
            {
                email: {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_at": "2026-10-01T00:00:00+00:00",
                }
            },
        )
        ctx.store.upsert_account(
            {"id": account_id, "provider": "google", "alias": "owner", "login": email, "status": "ok"}
        )
        calendar_id = account_id + ":primary"
        ctx.store.upsert_calendar(
            {
                "id": calendar_id,
                "account_id": account_id,
                "provider": "google",
                "external_id": "primary",
                "name": "Google",
                "writable": True,
            }
        )
        event = ctx.store.insert_event(
            {
                "calendar_id": calendar_id,
                "title": "Pending",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
            }
        )
        pending = ctx.store.add_intent("update", account_id, calendar_id, event["id"], {})
        conflict = ctx.store.add_intent("delete", account_id, calendar_id, event["id"], {})
        ctx.store.settle_intent(conflict["id"], "conflict", {"error": "conflict"})

        disconnected = json.loads(
            tools.cal_settings(ctx, action="disconnect", calendar_id=account_id, confirm=True)
        )
        reloaded = tools.reload_google(ctx)
        self.assertEqual(disconnected["cancelled_intents"], 2)
        self.assertTrue(disconnected["tokens_dropped"])
        self.assertEqual(ctx.store.open_intents(), [])
        self.assertIsNone(ctx.store.get_account(account_id))
        self.assertEqual(reloaded["status"], "not_connected")
        self.assertEqual(ctx.store.intents_for_event(event["id"])[0]["id"], pending["id"])


class ReminderRound3Tests(unittest.TestCase):
    def test_effective_exception_title_and_end_are_used(self):
        ctx = make_context()
        start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        master = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Master title",
                "start_utc": iso_utc(start),
                "end_utc": iso_utc(start + timedelta(hours=2)),
                "rrule": "FREQ=DAILY",
            }
        )
        ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Renamed occurrence",
                "master_id": master["id"],
                "recurrence_id": iso_utc(start),
                "start_utc": iso_utc(start),
                "end_utc": iso_utc(start + timedelta(minutes=10)),
            }
        )
        ctx.store.schedule_reminder(master["id"], iso_utc(start), 0, iso_utc(start), "cal:effective")
        channel = Channel()
        stats = rem.deliver_due(ctx.store, channel, UTC, now=start + timedelta(minutes=5))
        self.assertEqual(stats["sent"], 1)
        self.assertIn("Renamed occurrence", channel.sent[0][1])

    def test_catchup_delivers_running_recurring_occurrence(self):
        ctx = make_context()
        now = datetime(2026, 10, 1, 10, 30, tzinfo=UTC)
        start = now - timedelta(minutes=30)
        master = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Still running",
                "start_utc": iso_utc(start),
                "end_utc": iso_utc(now + timedelta(minutes=30)),
                "rrule": "FREQ=DAILY",
            }
        )
        ctx.store.schedule_reminder(
            master["id"],
            iso_utc(start),
            15,
            iso_utc(start - timedelta(minutes=15)),
            "cal:running",
        )
        channel = Channel()
        stats = rem.deliver_due(ctx.store, channel, UTC, now=now)
        self.assertEqual((stats["sent"], stats["batched"], len(channel.sent)), (1, 1, 1))

    def test_exception_reminder_edit_drops_master_slot_and_delivery_stops_after_five_retries(self):
        ctx = make_context()
        now = datetime.now(UTC).replace(microsecond=0)
        master = json.loads(
            tools.cal_create(
                ctx,
                title="Reminder series",
                start=(now + timedelta(hours=2)).isoformat(),
                duration_min=30,
                rrule="FREQ=DAILY",
                reminders=[15],
                confirm=True,
            )
        )["event"]
        key = master["start"]
        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@{iso_utc(now + timedelta(hours=2))}",
                reminders=[5],
                scope="this",
                confirm=True,
            )
        )
        rows = [
            row
            for row in ctx.store.upcoming_reminders(now, limit=100)
            if row["occurrence_start_utc"] == iso_utc(now + timedelta(hours=2))
        ]
        self.assertEqual({row["offset_min"] for row in rows}, {5})

        retry_event = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Retry exactly five",
                "start_utc": iso_utc(now + timedelta(minutes=10)),
                "end_utc": iso_utc(now + timedelta(hours=1)),
            }
        )
        ctx.store.schedule_reminder(
            retry_event["id"],
            retry_event["start_utc"],
            15,
            iso_utc(now - timedelta(minutes=5)),
            "cal:five",
        )
        channel = Channel(("retry", "HTTP 503"))
        for _ in range(4):
            rem.deliver_due(ctx.store, channel, UTC, now=now)
        self.assertEqual(len([row for row in ctx.store.due_reminders(now) if row["notice_id"] == "cal:five"]), 1)
        rem.deliver_due(ctx.store, channel, UTC, now=now)
        self.assertEqual([row for row in ctx.store.due_reminders(now) if row["notice_id"] == "cal:five"], [])


class GoogleRound3Tests(unittest.TestCase):
    def adapter(self):
        return GoogleAdapter(
            "owner@example.test",
            "token",
            "refresh",
            datetime.now(UTC) + timedelta(days=1),
            "client",
            "",
            None,
        )

    def test_all_day_google_exdate_roundtrip_uses_calendar_timezone_date(self):
        item = {
            "id": "series",
            "iCalUID": "series@example.test",
            "summary": "All day",
            "start": {"date": "2026-03-01"},
            "end": {"date": "2026-03-02"},
            "recurrence": ["RRULE:FREQ=WEEKLY", "EXDATE;VALUE=DATE:20260308"],
            "status": "confirmed",
        }
        row = gevent_to_row(item, "google:owner:primary", get_tz("Asia/Tokyo"))
        body = row_to_gevent(row)
        self.assertIn("EXDATE;VALUE=DATE:20260308", body["recurrence"])

    def test_instance_lookup_miss_stays_pending_for_retry(self):
        class MissingInstanceAdapter(RecordingAdapter):
            def update(self, calendar, event, expected_etag, payload):
                raise ops.ProviderError("retry", "instance not materialized yet")

        ctx = make_context()
        external = add_external_calendar(ctx.store, "google-miss", "full")
        ctx.providers = OneAdapterProviders(MissingInstanceAdapter())
        master = ctx.store.insert_event(
            {
                "calendar_id": external,
                "uid": "series@example.test",
                "external_id": "google-master",
                "title": "Series",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "rrule": "FREQ=DAILY",
            }
        )
        result = ops.update_event(
            ctx.store,
            ctx.providers,
            master["id"] + "@2026-10-02T09:00:00+00:00",
            {"title": "Exception"},
            scope="this",
        )
        self.assertEqual(result["assignments"][0]["status"], "pending")
        exception = ctx.store.exceptions_for(master["id"])[0]
        self.assertEqual(exception["sync_state"], "pending")
        self.assertEqual(ctx.store.open_intents()[0]["state"], "pending")

    def test_create_409_cancelled_resource_is_replaced_with_put(self):
        adapter = self.adapter()
        calls = []

        def request(method, path, params=None, body=None, headers=None, _retry=True):
            calls.append((method, path, params, body))
            if method == "POST":
                raise ops.ProviderError("http", "already exists", 409)
            if method == "GET":
                return 200, {}, {"id": path.rsplit("/", 1)[-1], "status": "cancelled", "etag": '"old"'}
            if method == "PUT":
                return 200, {}, {"id": path.rsplit("/", 1)[-1], "status": "confirmed", "etag": '"new"'}
            raise AssertionError(method)

        adapter._request = request
        result = adapter.create(
            {"external_id": "primary"},
            {
                "id": "local",
                "uid": "stable@example.test",
                "title": "Revive",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "tz": "UTC",
                "attendees_json": "[]",
                "reminders_json": "[]",
            },
            {},
        )
        self.assertEqual([call[0] for call in calls], ["POST", "GET", "PUT"])
        self.assertEqual(result["etag"], '"new"')


class YandexAndBoundedResultRound3Tests(unittest.TestCase):
    def test_yandex_master_update_preserves_foreign_recurrence_id_vevents(self):
        source = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"
            "BEGIN:VEVENT\r\nUID:series\r\nSUMMARY:Master\r\n"
            "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID:20261002T090000Z\r\nSUMMARY:Foreign moved\r\n"
            "DTSTART:20261002T120000Z\r\nDTEND:20261002T130000Z\r\nX-FOREIGN:keep-me\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID:20261003T090000Z\r\nSUMMARY:Foreign second\r\n"
            "DTSTART:20261003T110000Z\r\nDTEND:20261003T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        master = ics_to_rows(source, "yandex:owner:work", "https://example.test/series.ics", '"e1"', UTC)[0]
        adapter = YandexAdapter("owner@yandex.test", "password")
        puts = []
        adapter.get = lambda href: (source, '"e1"')
        adapter._request = lambda method, url, body=None, headers=None: (
            puts.append((method, url, body, headers)) or (204, {"etag": '"e2"'}, "")
        )
        adapter.update({}, {**master, "title": "Master changed"}, '"e1"', {})
        written = puts[-1][2]
        self.assertEqual(written.count("RECURRENCE-ID"), 2)
        self.assertIn("SUMMARY:Foreign moved", written)
        self.assertIn("SUMMARY:Foreign second", written)
        self.assertIn("X-FOREIGN:keep-me", written)

    def test_bounded_result_preserves_non_ok_status(self):
        from model import bounded_result

        text = bounded_result(
            {
                "status": "conflict",
                "message": "x" * 40_000,
                "changes": {"attendees": ["person-%05d@example.test" % index for index in range(3000)]},
            }
        )
        self.assertLessEqual(len(text), 15_000)
        self.assertEqual(json.loads(text)["status"], "conflict")


class WidgetRoutesRound3Tests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.ctx.tz = UTC
        self.api = RouteAPI()
        routes.register_routes(self.api, lambda: self.ctx)

    @staticmethod
    def payload(response):
        return json.loads(response.body.decode("utf-8"))

    def test_agenda_calendars_dash_returns_no_events(self):
        json.loads(
            tools.cal_create(
                self.ctx,
                title="Must be filtered",
                start="2026-10-01T09:00+00:00",
                confirm=True,
            )
        )
        response = asyncio.run(
            self.api.routes["agenda"](
                Request(query={"view": "day", "date": "2026-10-01", "calendars": "-"})
            )
        )
        self.assertEqual(self.payload(response)["events"], [])

    def test_week_route_has_seven_days_across_dst(self):
        self.ctx.tz = get_tz("Europe/Berlin")
        response = asyncio.run(
            self.api.routes["agenda"](
                Request(query={"view": "week", "date": "2026-03-25"})
            )
        )
        self.assertEqual(len(self.payload(response)["days"]), 7)

    def test_event_get_returns_requested_occurrence(self):
        event = make_series(self.ctx, [DEFAULT_LOCAL_CALENDAR_ID])
        occurrence_id = event["id"] + "@2026-10-08T09:00:00+00:00"
        response = asyncio.run(
            self.api.routes["event/get"](Request(query={"id": occurrence_id}))
        )
        payload = self.payload(response)
        self.assertEqual(payload["event"]["start"], "2026-10-08T09:00:00+00:00")
        self.assertEqual(payload["event"]["series_id"], event["id"])

    def test_reminders_save_sets_default_and_sync_sets_request_flag(self):
        reminder_response = asyncio.run(
            self.api.routes["reminders/save"](
                Request(body={"action": "set_default", "offsets": [17]})
            )
        )
        self.assertEqual(self.payload(reminder_response)["status"], "updated")
        self.assertEqual(rem.get_rules(self.ctx.store)["default"], [17])
        sync_response = asyncio.run(self.api.routes["sync"](Request(body={})))
        self.assertEqual(self.payload(sync_response)["status"], "accepted")
        self.assertTrue(self.ctx.store.get_setting("sync_requested_at"))


if __name__ == "__main__":
    unittest.main()
