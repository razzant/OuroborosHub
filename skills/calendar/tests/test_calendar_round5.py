"""Round 5 adversarial QA for calendar 1.0.6 (snapshot 7d8a44d).

All persistence lives in the temporary SQLite directories created by the test
helpers.  Provider and Host Service traffic is replaced with in-memory fakes.
Failures are review findings, not expected failures.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPTS = os.path.join(SKILL, "scripts")
for path in (SKILL, HERE, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import (  # noqa: E402  (also installs the Starlette stub)
    OneAdapterProviders,
    RecordingAdapter,
    add_external_calendar,
    make_context,
)

import ops  # noqa: E402
import tools  # noqa: E402
import worker as calendar_worker  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, parse_stored  # noqa: E402
from providers import YandexAdapter, ics_to_rows, row_to_ics  # noqa: E402
from providers_google import gevent_to_row, row_to_gevent  # noqa: E402


UTC = timezone.utc


class IntentRecoveryRound5Tests(unittest.TestCase):
    def test_new_owner_edit_can_recover_after_an_older_independent_write_failed(self):
        class FailOnce(RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.failed = False

            def update(self, calendar, event, expected_etag, payload):
                self.calls.append(("update", event["title"]))
                if not self.failed:
                    self.failed = True
                    raise ops.ProviderError("http", "bad first payload", 400)
                return {"etag": '"recovered"'}

        ctx = make_context()
        external = add_external_calendar(ctx.store, "intent-recovery")
        adapter = FailOnce()
        providers = OneAdapterProviders(adapter)
        event = ctx.store.insert_event(
            {
                "calendar_id": external,
                "uid": "intent-recovery@example.test",
                "external_id": "remote-recovery",
                "etag": '"old"',
                "title": "Original",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
            }
        )

        first = ops.update_event(ctx.store, providers, event["id"], {"title": "Rejected"}, scope="all")
        second = ops.update_event(ctx.store, providers, event["id"], {"title": "Corrected"}, scope="all")

        self.assertEqual(first["assignments"][0]["status"], "failed")
        self.assertEqual(second["assignments"][0]["status"], "done")
        self.assertEqual(adapter.calls, [("update", "Rejected"), ("update", "Corrected")])
        self.assertEqual([i["state"] for i in ctx.store.intents_for_event(event["id"])], ["failed", "done"])


class RecurrenceRound5Tests(unittest.TestCase):
    def test_all_day_following_split_shifts_dates_by_calendar_days_across_dst(self):
        ctx = make_context()
        tz = get_tz("Europe/Berlin")
        ctx.tz = tz
        master = json.loads(
            tools.cal_create(
                ctx,
                title="DST days",
                start="2026-10-24",
                all_day=True,
                rrule="FREQ=DAILY;COUNT=10",
                confirm=True,
            )
        )["event"]
        exdate = datetime(2026, 10, 27, tzinfo=tz)
        rdate = datetime(2026, 10, 28, tzinfo=tz)
        ctx.store.update_event(
            master["id"],
            {"exdates": iso_utc(exdate), "rdates": iso_utc(rdate)},
        )
        split_occurrence = ctx.occurrences(
            datetime(2026, 10, 25, tzinfo=tz),
            datetime(2026, 10, 26, tzinfo=tz),
        )[0]
        new_start = datetime(2026, 10, 26, tzinfo=tz)

        ops.update_event(
            ctx.store,
            ctx.providers,
            split_occurrence["id"],
            {
                "start_utc": iso_utc(new_start),
                "end_utc": iso_utc(new_start + timedelta(days=1)),
                "all_day": True,
            },
            scope="following",
        )

        masters = [
            row
            for row in ctx.store.window(
                datetime(2026, 10, 1, tzinfo=UTC),
                datetime(2026, 11, 15, tzinfo=UTC),
            )
            if row.get("rrule") and row["id"] != master["id"]
        ]
        self.assertEqual(len(masters), 1)
        shifted_ex = parse_stored(masters[0]["exdates"]).astimezone(tz)
        shifted_rd = parse_stored(masters[0]["rdates"]).astimezone(tz)
        self.assertEqual((shifted_ex.date().isoformat(), shifted_ex.hour), ("2026-10-28", 0))
        self.assertEqual((shifted_rd.date().isoformat(), shifted_rd.hour), ("2026-10-29", 0))
        self.assertIn("COUNT=9", masters[0]["rrule"])

    def test_yandex_split_rewrite_removes_late_rdate_from_old_master_and_writes_it_to_new_master(self):
        raw = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
            "UID:rdate-series\r\nSUMMARY:Series\r\nDTSTART:20261001T090000Z\r\n"
            "DTEND:20261001T100000Z\r\nRRULE:FREQ=DAILY\r\n"
            "RDATE:20261010T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        common = {
            "uid": "rdate-series",
            "title": "Series",
            "tz": "UTC",
            "all_day": 0,
            "attendees_json": "[]",
            "reminders_json": "[]",
        }
        old_ics = row_to_ics(
            {
                **common,
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "rrule": "FREQ=DAILY;UNTIL=20261004T085959Z",
                "rdates": "",
                "raw_payload": raw,
            }
        )
        new_ics = row_to_ics(
            {
                **common,
                "uid": "rdate-series-split",
                "start_utc": "2026-10-05T11:00:00+00:00",
                "end_utc": "2026-10-05T12:00:00+00:00",
                "rrule": "FREQ=DAILY",
                "rdates": "2026-10-10T11:00:00+00:00",
                "raw_payload": "",
            }
        )

        self.assertEqual(("RDATE" in old_ics, "RDATE" in new_ics), (False, True))
        self.assertIn("20261010T110000", new_ics)

    def test_google_rdate_survives_provider_roundtrip(self):
        item = {
            "id": "rdate-google",
            "iCalUID": "rdate-google@example.test",
            "summary": "RDATE",
            "start": {"dateTime": "2026-10-01T09:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-01T10:00:00Z", "timeZone": "UTC"},
            "recurrence": ["RRULE:FREQ=DAILY", "RDATE:20261010T090000Z"],
            "status": "confirmed",
        }
        row = gevent_to_row(item, "google:owner:primary", UTC)
        body = row_to_gevent(row)
        self.assertIn("RDATE:20261010T090000Z", body["recurrence"])


class YandexRound5Tests(unittest.TestCase):
    def test_all_day_rows_remember_the_timezone_used_for_date_values(self):
        raw = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
            "UID:date-zone\r\nSUMMARY:All day\r\nDTSTART;VALUE=DATE:20260301\r\n"
            "DTEND;VALUE=DATE:20260302\r\nRRULE:FREQ=WEEKLY\r\n"
            "EXDATE;VALUE=DATE:20260308\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        row = ics_to_rows(raw, "yandex:owner:work", "https://example.test/date.ics", '"e1"', get_tz("America/Los_Angeles"))[0]
        self.assertEqual(row["tz"], "America/Los_Angeles")

    def test_scope_all_shift_does_not_keep_the_old_live_exdate_as_an_extra_cancellation(self):
        raw = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
            "UID:shifted-days\r\nSUMMARY:Days\r\nDTSTART;VALUE=DATE:20261001\r\n"
            "DTEND;VALUE=DATE:20261002\r\nRRULE:FREQ=DAILY\r\n"
            "EXDATE;VALUE=DATE:20261003\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        adapter = YandexAdapter("owner@yandex.test", "password")
        puts = []
        adapter.get = lambda _href: (raw, '"e1"')
        adapter._request = lambda method, url, body=None, headers=None: (
            puts.append((method, url, body, headers)) or (204, {"etag": '"e2"'}, "")
        )
        adapter.update(
            {},
            {
                "uid": "shifted-days",
                "href": "https://caldav.yandex.ru/calendars/owner/shifted-days.ics",
                "etag": '"e1"',
                "title": "Days",
                "start_utc": "2026-10-02T00:00:00+00:00",
                "end_utc": "2026-10-03T00:00:00+00:00",
                "tz": "UTC",
                "all_day": 1,
                "rrule": "FREQ=DAILY",
                "exdates": "2026-10-04T00:00:00+00:00",
                "attendees_json": "[]",
                "reminders_json": "[]",
                "raw_payload": raw,
            },
            '"e1"',
            {},
        )
        rewritten = ics_to_rows(puts[0][2], "cal", "href", '"e2"', UTC)[0]
        self.assertEqual(rewritten["exdates"], "2026-10-04T00:00:00+00:00")


class FakeFetchAdapter:
    def __init__(self, rows, seen):
        self.rows = rows
        self.seen = seen

    def fetch(self, calendar, cursor="", tz=None):
        return self.rows, "", "window", list(self.seen)

    def exists(self, href):
        return href in self.seen


class WorkerReconcileRound5Tests(unittest.TestCase):
    def _linked_external_series(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store, "worker-linked", "full")
        ctx.providers = OneAdapterProviders(RecordingAdapter())
        created = json.loads(
            tools.cal_create(
                ctx,
                title="Worker linked",
                start="2026-10-01T09:00+00:00",
                duration_min=60,
                rrule="FREQ=DAILY",
                calendars=[external, DEFAULT_LOCAL_CALENDAR_ID],
                confirm=True,
            )
        )["event"]
        source = ctx.store.get_event(created["id"])
        sibling = next(
            row
            for row in ctx.store.group_masters(source["link_group_id"])
            if row["id"] != source["id"]
        )
        return ctx, external, source, sibling

    def test_external_moved_exception_propagates_to_the_linked_copy(self):
        ctx, external, source, _sibling = self._linked_external_series()
        key = "2026-10-02T09:00:00+00:00"
        master_row = {
            **source,
            "etag": source["etag"],
            "origin": "external",
        }
        exception_row = {
            "calendar_id": external,
            "uid": source["uid"],
            "external_id": "remote-instance",
            "href": source["href"],
            "etag": '"instance-2"',
            "title": "Moved outside",
            "description": "",
            "location": "",
            "start_utc": "2026-10-02T12:00:00+00:00",
            "end_utc": "2026-10-02T13:00:00+00:00",
            "tz": "UTC",
            "all_day": 0,
            "rrule": "",
            "exdates": "",
            "rdates": "",
            "recurrence_id": key,
            "master_external_id": source["external_id"],
            "status": "confirmed",
            "organizer": "",
            "attendees_json": "[]",
            "my_response": "",
            "reminders_json": "[]",
            "origin": "external",
            "availability": "busy",
            "sync_state": "synced",
            "raw_payload": "",
        }
        fake = FakeFetchAdapter([master_row, exception_row], [source["href"]])

        result = calendar_worker.reconcile_calendar(
            ctx.store,
            ctx.providers,
            fake,
            ctx.store.get_calendar(external),
            UTC,
        )
        cards = ctx.occurrences(
            datetime(2026, 10, 2, tzinfo=UTC),
            datetime(2026, 10, 3, tzinfo=UTC),
        )
        self.assertEqual(result[2], 1)
        self.assertEqual(len(cards), 2)
        self.assertEqual({card["start_utc"] for card in cards}, {"2026-10-02T12:00:00+00:00"})

    def test_yandex_exdate_cancellation_propagates_to_the_linked_copy(self):
        ctx, external, source, sibling = self._linked_external_series()
        key = "2026-10-02T09:00:00+00:00"
        remote_master = {
            **source,
            "etag": '"remote-2"',
            "exdates": key,
            "origin": "external",
        }
        fake = FakeFetchAdapter([remote_master], [source["href"]])

        calendar_worker.reconcile_calendar(
            ctx.store,
            ctx.providers,
            fake,
            ctx.store.get_calendar(external),
            UTC,
        )
        cards = ctx.occurrences(
            datetime(2026, 10, 2, tzinfo=UTC),
            datetime(2026, 10, 3, tzinfo=UTC),
        )
        self.assertEqual(ctx.store.get_event(source["id"])["exdates"], key)
        sib_row = ctx.store.get_event(sibling["id"])
        cancelled = [e for e in ctx.store.exceptions_for(sibling["id"]) if e.get("recurrence_id") == key and e.get("status") == "cancelled"]
        self.assertTrue(sib_row["exdates"] == key or cancelled)   # the copy's occurrence is cancelled (exdate or cancelled exception row)
        self.assertEqual(cards, [])


if __name__ == "__main__":
    unittest.main()
