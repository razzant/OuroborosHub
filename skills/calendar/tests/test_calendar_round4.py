"""Round 4 adversarial QA for calendar 1.0.5 (snapshot 87baa4d).

Every persistence test uses an isolated temporary SQLite state directory.
Failures are review findings and are deliberately not marked expected failures.
No live provider or host-service writes are made.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
for path in (SKILL, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import (  # noqa: E402  (also installs the Starlette stub)
    Channel,
    OneAdapterProviders,
    RecordingAdapter,
    Request,
    RouteAPI,
    add_external_calendar,
    add_local_calendar,
    make_context,
)
from test_calendar_round3 import make_series  # noqa: E402

import ops  # noqa: E402
import reminders as rem  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, now_utc  # noqa: E402
from providers import YandexAdapter, row_to_ics  # noqa: E402
from providers_google import GoogleAdapter, gevent_to_row, row_to_gevent  # noqa: E402
from store import Store  # noqa: E402


UTC = timezone.utc


def force_due(store, intent_id):
    store.settle_intent(intent_id, "pending", {}, retry_in_sec=0)


def live_occurrences(ctx, start, end, calendars=None):
    return json.loads(tools.cal_events(ctx, start=start, end=end, calendars=calendars))["events"]


class IntentOrderingRound4Tests(unittest.TestCase):
    def test_rsvp_waits_until_deferred_update_succeeds(self):
        class DeferredUpdate(RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.deferred = False

            def update(self, calendar, event, expected_etag, payload):
                self.calls.append(("update", event["id"]))
                if not self.deferred:
                    self.deferred = True
                    raise ops.ProviderError("retry", "write not visible yet")
                return {"etag": '"updated"'}

            def respond(self, calendar, event, payload):
                self.calls.append(("respond", event["id"]))
                return {"etag": '"rsvp"'}

        ctx = make_context()
        external = add_external_calendar(ctx.store, "rsvp-order")
        adapter = DeferredUpdate()
        ctx.providers = OneAdapterProviders(adapter)
        event = ctx.store.insert_event(
            {
                "calendar_id": external,
                "uid": "rsvp-order@example.test",
                "external_id": "remote-rsvp-order",
                "etag": '"old"',
                "title": "Invite",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "attendees_json": '[{"email":"owner@example.test","self":true}]',
                "origin": "external",
            }
        )

        ops.update_event(
            ctx.store,
            ctx.providers,
            event["id"],
            {"title": "Changed", "my_response": "accepted"},
            scope="all",
        )
        self.assertEqual(adapter.calls, [("update", event["id"])])
        intents = ctx.store.intents_for_event(event["id"])
        self.assertEqual([row["kind"] for row in intents], ["update", "rsvp"])
        self.assertEqual([row["state"] for row in intents], ["pending", "pending"])

        for intent in ctx.store.open_intents():
            force_due(ctx.store, intent["id"])
        ops.retry_due_intents(ctx.store, ctx.providers)
        self.assertEqual(adapter.calls, [("update", event["id"]), ("update", event["id"]), ("respond", event["id"])])

    def test_exception_write_waits_for_deferred_master_create(self):
        class DeferredCreate(RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.deferred = False

            def create(self, calendar, event, payload):
                self.calls.append(("create", event["id"]))
                if not self.deferred:
                    self.deferred = True
                    raise ops.ProviderError("network", "lost create response")
                return {"external_id": "remote-master", "href": "https://example.test/master.ics", "etag": '"m1"'}

            def update(self, calendar, event, expected_etag, payload):
                self.calls.append(("update", event["id"], event.get("master_external_id")))
                return {"etag": '"x1"', "external_id": "remote-instance"}

        ctx = make_context()
        external = add_external_calendar(ctx.store, "exception-order")
        adapter = DeferredCreate()
        ctx.providers = OneAdapterProviders(adapter)
        source = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID])
        json.loads(
            tools.cal_update(
                ctx,
                id=f"{source['id']}@2026-10-03T09:00:00+00:00",
                title="Special",
                scope="this",
                confirm=True,
            )
        )

        ops.reassign_event(
            ctx.store,
            ctx.providers,
            ctx.store.get_event(source["id"]),
            [DEFAULT_LOCAL_CALENDAR_ID, external],
        )
        self.assertEqual([call[0] for call in adapter.calls], ["create"])
        for intent in ctx.store.open_intents():
            force_due(ctx.store, intent["id"])
        ops.retry_due_intents(ctx.store, ctx.providers)
        self.assertEqual([call[0] for call in adapter.calls], ["create", "create", "update"])
        self.assertEqual(adapter.calls[-1][2], "remote-master")

    def test_two_workers_cannot_execute_the_same_intent_under_one_lease(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingAdapter(RecordingAdapter):
            def update(self, calendar, event, expected_etag, payload):
                self.calls.append(("update", event["id"]))
                entered.set()
                release.wait(2)
                return {"etag": '"done"'}

        ctx = make_context()
        external = add_external_calendar(ctx.store, "lease")
        adapter = BlockingAdapter()
        providers = OneAdapterProviders(adapter)
        event = ctx.store.insert_event(
            {
                "calendar_id": external,
                "external_id": "remote-lease",
                "title": "Lease",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
            }
        )
        account_id = ctx.store.get_calendar(external)["account_id"]
        intent = ctx.store.add_intent("update", account_id, external, event["id"], {})
        result = {}

        def first_worker():
            result["first"] = ops.execute_intent(ctx.store, providers, intent, "worker-a")

        thread = threading.Thread(target=first_worker)
        thread.start()
        self.assertTrue(entered.wait(2))
        result["second"] = ops.execute_intent(Store(ctx.state_dir), providers, intent, "worker-b")
        release.set()
        thread.join(2)

        self.assertEqual(result["second"]["status"], "pending")
        self.assertEqual(result["first"]["status"], "done")
        self.assertEqual(adapter.calls, [("update", event["id"])])

    def test_retry_kind_stops_after_five_leases(self):
        class AlwaysRetry(RecordingAdapter):
            def update(self, calendar, event, expected_etag, payload):
                self.calls.append(("update", event["id"]))
                raise ops.ProviderError("retry", "not materialized")

        ctx = make_context()
        external = add_external_calendar(ctx.store, "bounded-retry")
        adapter = AlwaysRetry()
        providers = OneAdapterProviders(adapter)
        event = ctx.store.insert_event(
            {
                "calendar_id": external,
                "external_id": "remote-retry",
                "title": "Retry",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
            }
        )
        account_id = ctx.store.get_calendar(external)["account_id"]
        intent = ctx.store.add_intent("update", account_id, external, event["id"], {})
        for _ in range(10):
            current = ctx.store.intents_for_event(event["id"])[0]
            if current["state"] == "failed":
                break
            force_due(ctx.store, intent["id"])
            ops.retry_due_intents(ctx.store, providers)
        saved = ctx.store.intents_for_event(event["id"])[0]
        self.assertEqual((saved["state"], saved["attempts"], len(adapter.calls)), ("failed", 5, 5))

    def test_gone_delete_is_idempotent_success_in_run_leased_intent(self):
        class GoneDelete(RecordingAdapter):
            def delete(self, calendar, event, expected_etag, payload=None):
                raise ops.ProviderError("gone", "already gone", 410)

        ctx = make_context()
        external = add_external_calendar(ctx.store, "gone")
        event = ctx.store.insert_event(
            {
                "calendar_id": external,
                "external_id": "remote-gone",
                "title": "Gone",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
            }
        )
        account_id = ctx.store.get_calendar(external)["account_id"]
        intent = ctx.store.add_intent("delete", account_id, external, event["id"], {})
        leased = ctx.store.lease_intent(intent["id"], "round4")
        result = ops.run_leased_intent(ctx.store, OneAdapterProviders(GoneDelete()), leased)
        self.assertEqual(result["status"], "done")
        self.assertIsNone(ctx.store.get_event(event["id"]))
        self.assertEqual(ctx.store.intents_for_event(event["id"])[0]["state"], "done")


class RecurrenceIdentityRound4Tests(unittest.TestCase):
    def _linked_exception(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "linked-second", "full")
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        key = "2026-10-05T09:00:00+00:00"
        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@{key}",
                title="Special occurrence",
                scope="this",
                confirm=True,
            )
        )
        exception_ids = [
            exc["id"]
            for linked_master in ctx.store.group_masters(master["link_group_id"])
            for exc in ctx.store.exceptions_for(linked_master["id"])
        ]
        return ctx, master, second, key, exception_ids

    def test_scope_all_from_saved_exception_remaps_linked_exception_slots(self):
        ctx, master, second, _key, exception_ids = self._linked_exception()
        json.loads(
            tools.cal_update(
                ctx,
                id=exception_ids[0],
                start="2026-10-05T11:00+00:00",
                scope="all",
                confirm=True,
            )
        )

        cards = live_occurrences(
            ctx,
            "2026-10-05",
            "2026-10-06",
            [DEFAULT_LOCAL_CALENDAR_ID, second],
        )
        self.assertEqual(len(cards), 2)
        self.assertEqual({card["start"][11:16] for card in cards}, {"11:00"})
        for linked_master in ctx.store.group_masters(master["link_group_id"]):
            exceptions = ctx.store.exceptions_for(linked_master["id"])
            self.assertEqual([row["recurrence_id"] for row in exceptions], ["2026-10-05T11:00:00+00:00"])

    def test_scope_all_shift_remaps_exdates_on_every_linked_copy(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "all-exdate", "full")
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        for linked_master in ctx.store.group_masters(master["link_group_id"]):
            ctx.store.update_event(linked_master["id"], {"exdates": "2026-10-05T09:00:00+00:00"})

        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@2026-10-10T09:00:00+00:00",
                start="2026-10-10T11:00+00:00",
                scope="all",
                confirm=True,
            )
        )
        self.assertEqual(
            live_occurrences(ctx, "2026-10-05", "2026-10-06", [DEFAULT_LOCAL_CALENDAR_ID, second]),
            [],
        )
        self.assertEqual(
            {row["exdates"] for row in ctx.store.group_masters(master["link_group_id"])},
            {"2026-10-05T11:00:00+00:00"},
        )

    def test_following_from_saved_exception_applies_split_time_to_that_occurrence_and_copies(self):
        ctx, _master, second, _key, exception_ids = self._linked_exception()
        json.loads(
            tools.cal_update(
                ctx,
                id=exception_ids[0],
                start="2026-10-05T10:30+00:00",
                scope="following",
                confirm=True,
            )
        )

        first = live_occurrences(
            ctx,
            "2026-10-05",
            "2026-10-06",
            [DEFAULT_LOCAL_CALENDAR_ID, second],
        )
        later = live_occurrences(
            ctx,
            "2026-10-07",
            "2026-10-08",
            [DEFAULT_LOCAL_CALENDAR_ID, second],
        )
        self.assertEqual(len(first), 2)
        self.assertEqual({card["start"][11:16] for card in first}, {"10:30"})
        self.assertEqual({card["start"][11:16] for card in later}, {"10:30"})

    def test_following_shift_moves_late_exdates_on_all_new_linked_masters(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "following-exdate", "full")
        master = make_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        for linked_master in ctx.store.group_masters(master["link_group_id"]):
            ctx.store.update_event(
                linked_master["id"],
                {"exdates": "2026-10-02T09:00:00+00:00,2026-10-06T09:00:00+00:00"},
            )

        json.loads(
            tools.cal_update(
                ctx,
                id=f"{master['id']}@2026-10-03T09:00:00+00:00",
                start="2026-10-03T12:00+00:00",
                scope="following",
                confirm=True,
            )
        )
        new_group = live_occurrences(ctx, "2026-10-04", "2026-10-05")[0]["link_group_id"]
        new_masters = ctx.store.group_masters(new_group)
        self.assertEqual(len(new_masters), 2)
        self.assertEqual({row["exdates"] for row in new_masters}, {"2026-10-06T12:00:00+00:00"})
        self.assertEqual(live_occurrences(ctx, "2026-10-06", "2026-10-07"), [])


class YandexRound4Tests(unittest.TestCase):
    @staticmethod
    def series_ics():
        return (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"
            "BEGIN:VEVENT\r\nUID:series\r\nSUMMARY:Master\r\n"
            "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID:20261003T090000Z\r\nSUMMARY:Early\r\n"
            "DTSTART:20261003T110000Z\r\nDTEND:20261003T120000Z\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID:20261010T090000Z\r\nSUMMARY:Late\r\n"
            "DTSTART:20261010T110000Z\r\nDTEND:20261010T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )

    @staticmethod
    def master_row(raw):
        return {
            "uid": "series",
            "href": "https://caldav.yandex.ru/calendars/owner/series.ics",
            "etag": '"cached"',
            "title": "Changed",
            "start_utc": "2026-10-01T09:00:00+00:00",
            "end_utc": "2026-10-01T10:00:00+00:00",
            "tz": "UTC",
            "all_day": 0,
            "rrule": "FREQ=DAILY;UNTIL=20261005T085959Z",
            "attendees_json": "[]",
            "reminders_json": "[]",
            "raw_payload": raw,
        }

    def test_series_update_conflicts_before_put_when_live_etag_diverged(self):
        raw = self.series_ics()
        adapter = YandexAdapter("owner@yandex.test", "password")
        puts = []
        adapter.get = lambda href: (raw, '"live-new"')
        adapter._request = lambda *args, **kwargs: puts.append((args, kwargs))

        with self.assertRaises(ops.ProviderError) as caught:
            adapter.update({}, self.master_row(raw), '"cached"', {})
        self.assertEqual(caught.exception.kind, "conflict")
        self.assertEqual(puts, [])

    def test_series_update_uses_live_matching_etag_and_prunes_exceptions_after_until(self):
        raw = self.series_ics()
        adapter = YandexAdapter("owner@yandex.test", "password")
        puts = []
        adapter.get = lambda href: (raw, '"cached"')
        adapter._request = lambda method, url, body=None, headers=None: (
            puts.append((method, url, body, headers)) or (204, {"etag": '"new"'}, "")
        )

        result = adapter.update({}, self.master_row(raw), '"cached"', {})
        self.assertEqual(result["etag"], '"new"')
        self.assertEqual(puts[0][3]["If-Match"], '"cached"')
        self.assertIn("SUMMARY:Early", puts[0][2])
        self.assertNotIn("SUMMARY:Late", puts[0][2])

    def test_all_day_exception_writes_date_exdate_and_missing_master_href_retries(self):
        raw = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:days\r\n"
            "SUMMARY:Days\r\nDTSTART;VALUE=DATE:20261001\r\nDTEND;VALUE=DATE:20261002\r\n"
            "RRULE:FREQ=DAILY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        adapter = YandexAdapter("owner@yandex.test", "password")
        puts = []
        adapter.get = lambda href: (raw, '"d1"')
        adapter._request = lambda method, url, body=None, headers=None: (
            puts.append((method, url, body, headers)) or (204, {"etag": '"d2"'}, "")
        )
        event = {
            "master_id": "local-master",
            "master_href": "https://caldav.yandex.ru/calendars/owner/days.ics",
            "recurrence_id": "2026-10-08T00:00:00+00:00",
            "uid": "days",
            "title": "Days",
            "start_utc": "2026-10-08T00:00:00+00:00",
            "end_utc": "2026-10-09T00:00:00+00:00",
            "tz": "UTC",
            "all_day": 1,
            "status": "cancelled",
            "attendees_json": "[]",
            "reminders_json": "[]",
        }
        adapter.update({}, event, "", {})
        self.assertIn("EXDATE;VALUE=DATE:20261008", puts[0][2])
        with self.assertRaises(ops.ProviderError) as caught:
            adapter.update({}, {**event, "master_href": ""}, "", {})
        self.assertEqual(caught.exception.kind, "retry")

    def test_calendar_without_privilege_set_is_writable(self):
        xml = (
            '<?xml version="1.0"?>'
            '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:response><d:href>/calendars/owner/work/</d:href><d:propstat><d:prop>'
            '<d:displayname>Work</d:displayname><d:resourcetype><d:collection/><c:calendar/>'
            '</d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>'
        )
        adapter = YandexAdapter("owner@yandex.test", "password")
        adapter._request = lambda *args, **kwargs: (207, {}, xml)
        calendars = adapter.list_calendars()
        self.assertEqual(len(calendars), 1)
        self.assertTrue(calendars[0]["writable"])
        self.assertEqual(calendars[0]["access_role"], "owner")


class GoogleRound4Tests(unittest.TestCase):
    @staticmethod
    def adapter():
        return GoogleAdapter(
            "owner@example.test",
            "token",
            "refresh",
            datetime.now(UTC) + timedelta(days=1),
            "client",
            "",
            None,
        )

    def test_repeated_edit_of_same_occurrence_keeps_master_identity_enrichment(self):
        class GoogleLikeAdapter(RecordingAdapter):
            def update(self, calendar, event, expected_etag, payload):
                if not event.get("external_id"):
                    self.assert_master(event)
                    external_id = event["master_external_id"] + "_20261003T090000Z"
                else:
                    self.assert_master(event)
                    external_id = event["external_id"]
                self.calls.append((event["id"], event["master_external_id"], external_id))
                return {"etag": '"instance"', "external_id": external_id}

            @staticmethod
            def assert_master(event):
                if event.get("master_external_id") != "google-master":
                    raise AssertionError("master identity was not enriched")

        ctx = make_context()
        external = add_external_calendar(ctx.store, "google-repeat")
        adapter = GoogleLikeAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        master = ctx.store.insert_event(
            {
                "calendar_id": external,
                "uid": "google-series@example.test",
                "external_id": "google-master",
                "etag": '"m1"',
                "title": "Series",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "rrule": "FREQ=DAILY",
                "origin": "external",
            }
        )
        occurrence_id = master["id"] + "@2026-10-03T09:00:00+00:00"
        ops.update_event(ctx.store, ctx.providers, occurrence_id, {"title": "First"}, scope="this")
        exception = ctx.store.exceptions_for(master["id"])[0]
        ops.update_event(ctx.store, ctx.providers, exception["id"], {"title": "Second"}, scope="this")

        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual({call[1] for call in adapter.calls}, {"google-master"})
        self.assertEqual(adapter.calls[0][2], adapter.calls[1][2])

    def test_create_409_cancelled_is_replaced_with_put(self):
        adapter = self.adapter()
        calls = []

        def request(method, path, params=None, body=None, headers=None, _retry=True):
            calls.append((method, path, body))
            if method == "POST":
                raise ops.ProviderError("http", "duplicate id", 409)
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
        self.assertNotIn("id", calls[-1][2])
        self.assertEqual(result["etag"], '"new"')

    def test_all_day_exdate_roundtrip_respects_read_timezone(self):
        item = {
            "id": "all-day",
            "iCalUID": "all-day@example.test",
            "summary": "All day",
            "start": {"date": "2026-03-01"},
            "end": {"date": "2026-03-02"},
            "recurrence": ["RRULE:FREQ=WEEKLY", "EXDATE;VALUE=DATE:20260308"],
            "status": "confirmed",
        }
        row = gevent_to_row(item, "google:owner:primary", get_tz("America/Los_Angeles"))
        body = row_to_gevent(row)
        self.assertEqual(row["tz"], "America/Los_Angeles")
        self.assertIn("EXDATE;VALUE=DATE:20260308", body["recurrence"])


class ReminderRound4Tests(unittest.TestCase):
    def test_soft_series_delivery_uses_owner_timezone_after_zone_change(self):
        ctx = make_context()
        ctx.tz = get_tz("Asia/Dubai")
        master = json.loads(
            tools.cal_create(
                ctx,
                title="Usually breakfast",
                start="2026-10-01T06:30",
                duration_min=30,
                availability="soft",
                rrule="FREQ=DAILY",
                reminders=[0],
                confirm=True,
            )
        )["event"]
        new_tz = get_tz("Europe/Moscow")
        now = datetime(2026, 10, 2, 6, 30, tzinfo=new_tz)
        ctx.tz = new_tz
        ctx.store.drop_reminders_for(master["id"], only_future=False)
        rem.plan(
            ctx.store,
            lambda start, end: ops.expand(
                ctx.store.window(start, end, include_hidden=True),
                start,
                end,
                ctx.store.exceptions_for,
                owner_tz=new_tz,
            ),
            now=now - timedelta(seconds=1),
        )
        channel = Channel()
        stats = rem.deliver_due(ctx.store, channel, new_tz, now=now)
        self.assertEqual(stats["sent"], 1)
        self.assertIn("06:30", channel.sent[0][1])

    def test_delivery_caps_notice_at_1000_and_title_at_120_characters(self):
        ctx = make_context()
        now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        event = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "T" * 500,
                "location": "L" * 10_000,
                "start_utc": iso_utc(now),
                "end_utc": iso_utc(now + timedelta(hours=1)),
            }
        )
        ctx.store.schedule_reminder(event["id"], event["start_utc"], 0, event["start_utc"], "cal:limits")
        channel = Channel()
        rem.deliver_due(ctx.store, channel, UTC, now=now)
        text = channel.sent[0][1]
        self.assertLessEqual(len(text), 1000)
        self.assertIn("T" * 120, text)
        self.assertNotIn("T" * 121, text)

    def test_token_rejected_is_distinct_and_status_explains_recovery(self):
        class RejectingChannel(rem.NotifyChannel):
            def _request(self, method, path, body=None):
                raise rem._HttpError(403, "stale token")

        channel = RejectingChannel("http://127.0.0.1:9", "secret")
        self.assertEqual(channel.state(refresh=True), "token_rejected")
        self.assertEqual(channel.send("cal:test", "text"), ("no_route", "token_rejected"))   # kept as no_channel, no attempts burnt

        ctx = make_context()
        ctx.store.set_setting("notify_channel_state", {"state": "token_rejected"})
        status = json.loads(tools.cal_status(ctx))
        self.assertEqual(status["reminders"]["channel"]["state"], "token_rejected")
        self.assertTrue(any("аттеста" in step and "toggle" in step for step in status["next_step"]))


class WidgetAndReadOnlyRound4Tests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.ctx.tz = UTC
        self.api = RouteAPI()
        routes.register_routes(self.api, lambda: self.ctx)

    @staticmethod
    def payload(response):
        return json.loads(response.body.decode("utf-8"))

    def test_agenda_dash_is_empty_and_week_has_seven_days(self):
        json.loads(
            tools.cal_create(
                self.ctx,
                title="Filtered",
                start="2026-03-25T09:00+00:00",
                confirm=True,
            )
        )
        empty = asyncio.run(
            self.api.routes["agenda"](
                Request(query={"view": "day", "date": "2026-03-25", "calendars": "-"})
            )
        )
        self.assertEqual(self.payload(empty)["events"], [])

        self.ctx.tz = get_tz("Europe/Berlin")
        week = asyncio.run(
            self.api.routes["agenda"](
                Request(query={"view": "week", "date": "2026-03-25"})
            )
        )
        self.assertEqual(len(self.payload(week)["days"]), 7)

    def test_reassign_clicked_read_only_linked_copy_refuses_without_mutation(self):
        external = add_external_calendar(self.ctx.store, "read-only-copy", "full")
        adapter = RecordingAdapter()
        self.ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(
            tools.cal_create(
                self.ctx,
                title="Linked",
                start="2026-10-01T09:00+00:00",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, external],
                confirm=True,
            )
        )["event"]
        external_copy = next(
            row
            for row in self.ctx.store.group_masters(created["link_group_id"])
            if row["calendar_id"] == external
        )
        self.ctx.store.upsert_calendar(
            {
                **self.ctx.store.get_calendar(external),
                "writable": False,
            }
        )
        third = add_local_calendar(self.ctx.store, "must-not-be-added", "full")
        before_counts = self.ctx.store.counts()
        before_members = {
            row["id"]: (row["calendar_id"], row["deleted_at"], row["link_group_id"])
            for row in self.ctx.store.group_members(created["link_group_id"])
        }

        result = ops.reassign_event(
            self.ctx.store,
            self.ctx.providers,
            self.ctx.store.get_event(external_copy["id"]),
            [DEFAULT_LOCAL_CALENDAR_ID, external, third],
        )

        after_members = {
            row["id"]: (row["calendar_id"], row["deleted_at"], row["link_group_id"])
            for row in self.ctx.store.group_members(created["link_group_id"])
        }
        self.assertIn("failed", result)
        self.assertEqual(self.ctx.store.counts(), before_counts)
        self.assertEqual(after_members, before_members)


if __name__ == "__main__":
    unittest.main()
