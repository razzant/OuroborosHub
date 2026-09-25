"""Regression tests for review round 2 fixes (batch 3, calendar 1.0.4)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
for p in (SKILL, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_calendar_round2 import OneAdapterProviders, RecordingAdapter, add_external_calendar, make_context  # noqa: E402  (installs the starlette stub)

import ops  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, parse_stored  # noqa: E402
from providers_google import row_to_gevent  # noqa: E402


class FailingCreateAdapter(RecordingAdapter):
    def create(self, calendar, event, payload):
        raise ops.ProviderError("network", "simulated network failure")


class OwnerTimezoneTests(unittest.TestCase):
    def test_soft_routine_follows_owner_timezone_through_context(self):
        ctx = make_context()
        ctx.store.set_setting("timezone", "Asia/Dubai")
        ctx.tz = get_tz("Asia/Dubai")
        json.loads(tools.cal_create(ctx, title="обычно завтрак", start="2026-10-01T06:30", duration_min=30, hidden=True, availability="soft",
                                    rrule="FREQ=DAILY", confirm=True))
        ctx.store.set_setting("timezone", "Europe/Moscow")
        ctx.tz = get_tz("Europe/Moscow")
        day = datetime(2026, 10, 5, tzinfo=ctx.tz)
        occs = [o for o in ctx.occurrences(day, day + timedelta(days=1)) if o.get("availability") == "soft"]
        self.assertEqual(len(occs), 1)
        local = parse_stored(occs[0]["start_utc"]).astimezone(ctx.tz)
        self.assertEqual((local.hour, local.minute), (6, 30))


class ScopeSemanticsTests(unittest.TestCase):
    def test_following_without_occurrence_id_is_refused(self):
        ctx = make_context()
        created = json.loads(tools.cal_create(ctx, title="Серия", start="2026-10-01T09:00+00:00", rrule="FREQ=DAILY", confirm=True))
        u = json.loads(tools.cal_update(ctx, id=created["event"]["id"], start="2026-10-01T10:00+00:00", scope="following", confirm=True))
        self.assertEqual(u["status"], "error")
        d = json.loads(tools.cal_delete(ctx, id=created["event"]["id"], scope="following", confirm=True))
        self.assertEqual(d["status"], "error")
        self.assertIsNotNone(ctx.store.get_event(created["event"]["id"]))

    def test_scope_all_from_a_later_occurrence_shifts_the_series_by_the_delta(self):
        ctx = make_context()
        created = json.loads(tools.cal_create(ctx, title="Серия", start="2026-10-01T09:00+00:00", duration_min=60, rrule="FREQ=DAILY", confirm=True))
        occ_id = created["event"]["id"] + "@" + iso_utc(datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc))
        u = json.loads(tools.cal_update(ctx, id=occ_id, start="2026-10-10T11:30+00:00", scope="all", confirm=True))
        self.assertEqual(u["status"], "updated")
        master = ctx.store.get_event(created["event"]["id"])
        self.assertEqual(master["start_utc"], iso_utc(datetime(2026, 10, 1, 11, 30, tzinfo=timezone.utc)))
        self.assertEqual(master["end_utc"], iso_utc(datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)))
        first = ctx.occurrences(datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(len([o for o in first if o.get("series_id") == master["id"] or o["id"].startswith(master["id"])]), 1)

    def test_all_day_series_truncation_uses_a_date_until(self):
        ctx = make_context()
        created = json.loads(tools.cal_create(ctx, title="Отпуск", start="2026-10-01", all_day=True, rrule="FREQ=WEEKLY", confirm=True))
        occ_id = created["event"]["id"] + "@" + iso_utc(parse_stored(ctx.store.get_event(created["event"]["id"])["start_utc"]) + timedelta(days=14))
        d = json.loads(tools.cal_delete(ctx, id=occ_id, scope="following", confirm=True))
        self.assertEqual(d["status"], "deleted")
        rrule = ctx.store.get_event(created["event"]["id"])["rrule"]
        until = [p for p in rrule.split(";") if p.startswith("UNTIL=")][0][6:]
        self.assertEqual(len(until), 8, rrule)   # YYYYMMDD, a DATE like DTSTART

    def test_description_can_be_cleared_with_an_empty_string(self):
        ctx = make_context()
        created = json.loads(tools.cal_create(ctx, title="Т", start="2026-10-01T09:00+00:00", description="старое", location="там", confirm=True))
        json.loads(tools.cal_update(ctx, id=created["event"]["id"], description="", confirm=True))
        row = ctx.store.get_event(created["event"]["id"])
        self.assertEqual((row["description"], row["location"]), ("", "там"))


class ExternalWriteTests(unittest.TestCase):
    def test_mixed_rsvp_and_edit_sends_update_then_rsvp(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Встреча", start="2026-10-01T09:00+00:00", calendars=[external],
                                              attendees=["owner@example.test"], confirm=True))
        adapter.calls.clear()
        u = json.loads(tools.cal_update(ctx, id=created["event"]["id"], title="Встреча 2", response="accepted", confirm=True))
        self.assertEqual(u["status"], "updated")
        kinds = [c[0] for c in adapter.calls]
        self.assertEqual(kinds, ["update", "respond"])
        self.assertEqual(ctx.store.get_event(created["event"]["id"])["title"], "Встреча 2")

    def test_disconnect_cancels_pending_intents(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        ctx.providers = OneAdapterProviders(FailingCreateAdapter())
        created = json.loads(tools.cal_create(ctx, title="Уйдёт в pending", start="2026-10-01T09:00+00:00", calendars=[external], confirm=True))
        self.assertEqual(created["status"], "pending")
        account_id = ctx.store.get_calendar(external)["account_id"]
        r = json.loads(tools.cal_settings(ctx, action="disconnect", calendar_id=account_id, confirm=True))
        self.assertEqual(r["status"], "updated")
        self.assertEqual(r["cancelled_intents"], 1)
        self.assertEqual(ctx.store.open_intents(), [])

    def test_exception_rows_never_send_recurrence_to_google(self):
        body = row_to_gevent({"title": "x", "start_utc": "2026-10-01T09:00:00+00:00", "end_utc": "2026-10-01T10:00:00+00:00", "tz": "UTC",
                              "all_day": 0, "rrule": "", "master_id": "evt_master", "recurrence_id": "2026-10-01T09:00:00+00:00",
                              "attendees_json": "[]", "reminders_json": "[]"})
        self.assertNotIn("recurrence", body)
        master = row_to_gevent({"title": "x", "start_utc": "2026-10-01T09:00:00+00:00", "end_utc": "2026-10-01T10:00:00+00:00", "tz": "UTC",
                                "all_day": 0, "rrule": "FREQ=DAILY", "attendees_json": "[]", "reminders_json": "[]"})
        self.assertEqual(master["recurrence"], ["RRULE:FREQ=DAILY"])


class ReminderAndWidgetTests(unittest.TestCase):
    def test_changing_exception_reminders_drops_the_master_queue_for_that_date(self):
        ctx = make_context()
        from model import now_utc
        start = (now_utc() + timedelta(hours=2)).replace(microsecond=0)
        created = json.loads(tools.cal_create(ctx, title="Серия", start=start.isoformat(), duration_min=30, rrule="FREQ=HOURLY", reminders=[15], confirm=True))
        master_id = created["event"]["id"]
        key = iso_utc(start + timedelta(hours=3))
        before = [r for r in ctx.store.upcoming_reminders(start, limit=500) if r["occurrence_start_utc"] == key]
        self.assertEqual({r["offset_min"] for r in before}, {15})   # planned by cal_create
        json.loads(tools.cal_update(ctx, id=f"{master_id}@{key}", reminders=[5], scope="this", confirm=True))
        rows = [r for r in ctx.store.upcoming_reminders(start, limit=500) if r["occurrence_start_utc"] == key]
        self.assertEqual({r["offset_min"] for r in rows}, {5})   # the master's 15-minute row for that date is gone

    def test_week_across_dst_start_still_has_seven_days(self):
        ctx = make_context()
        ctx.tz = get_tz("Europe/Berlin")
        payload = routes.agenda_payload(ctx, "week", "2026-03-25", None, False)
        self.assertEqual(len(payload["days"]), 7)
        self.assertEqual(payload["days"][0]["date"], "2026-03-23")


if __name__ == "__main__":
    unittest.main()
