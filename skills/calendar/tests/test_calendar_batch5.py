"""Regression tests for the round-4 (final gate) findings — batch 5, calendar 1.0.6."""

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

from test_calendar_round2 import OneAdapterProviders, RecordingAdapter, add_external_calendar, add_local_calendar, make_context  # noqa: E402

import ops  # noqa: E402
import reminders as rem  # noqa: E402
import tools  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, now_utc, parse_stored  # noqa: E402
from providers import ics_to_rows, row_to_ics  # noqa: E402


class YandexAllDayTests(unittest.TestCase):
    def test_all_day_exdate_is_read_in_the_event_zone_and_excludes_the_occurrence(self):
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:s1\r\nSUMMARY:Отпуск\r\n"
               "DTSTART;VALUE=DATE:20261001\r\nDTEND;VALUE=DATE:20261002\r\nRRULE:FREQ=DAILY\r\nEXDATE;VALUE=DATE:20261003\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        tz = get_tz("Asia/Dubai")
        row = ics_to_rows(ics, "cal", "href", '"e1"', tz)[0]
        start = parse_stored(row["start_utc"])
        occs = ops.expand([{**row, "id": "m"}], start, start + timedelta(days=4), lambda _m: [])
        days = sorted(parse_stored(o["start_utc"]).astimezone(tz).date().isoformat() for o in occs)
        self.assertEqual(days, ["2026-10-01", "2026-10-02", "2026-10-04"])

    def test_master_rewrite_keeps_exdates_that_live_only_on_the_server(self):
        raw = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:s1\r\nSUMMARY:Серия\r\nDTSTART:20261001T090000Z\r\n"
               "DTEND:20261001T100000Z\r\nRRULE:FREQ=DAILY\r\nEXDATE:20261003T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        out = row_to_ics({"uid": "s1", "title": "Серия 2", "start_utc": "2026-10-01T09:00:00+00:00", "end_utc": "2026-10-01T10:00:00+00:00", "tz": "UTC",
                          "all_day": 0, "rrule": "FREQ=DAILY", "exdates": "", "raw_payload": raw, "attendees_json": "[]", "reminders_json": "[]"})
        self.assertIn("EXDATE", out)
        self.assertIn("20261003T090000", out)


class IntentDependencyTests(unittest.TestCase):
    def test_failed_predecessor_fails_the_dependent_instead_of_running_it(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)

        class FailingUpdate(RecordingAdapter):
            def update(self, calendar, event, expected_etag, payload):
                self.calls.append(("update", event.get("title")))
                raise ops.ProviderError("http", "HTTP 400")
        adapter = FailingUpdate()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Встреча", start="2026-10-01T09:00+00:00", calendars=[external],
                                              attendees=["owner@example.test"], confirm=True))
        adapter.calls.clear()
        u = json.loads(tools.cal_update(ctx, id=created["event"]["id"], title="Встреча 2", response="accepted", confirm=True))
        self.assertIn(u["status"], ("updated_partially", "conflict", "pending", "failed"))
        self.assertEqual([c[0] for c in adapter.calls], ["update"])   # respond was never attempted
        states = sorted((i["kind"], i["state"]) for i in ctx.store.intents_for_event(created["event"]["id"]))
        self.assertIn(("rsvp", "failed"), states)

    def test_waiting_for_the_master_does_not_burn_attempts(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)

        class NetworkOnce(RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.fail_creates = 1
            def create(self, calendar, event, payload):
                if self.fail_creates:
                    self.fail_creates -= 1
                    raise ops.ProviderError("network", "offline")
                return super().create(calendar, event, payload)
        ctx.providers = OneAdapterProviders(NetworkOnce())
        created = json.loads(tools.cal_create(ctx, title="Серия", start="2026-10-01T09:00+00:00", rrule="FREQ=DAILY", calendars=[external], confirm=True))
        key = iso_utc(datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc))
        json.loads(tools.cal_update(ctx, id=f"{created['event']['id']}@{key}", title="Позже", scope="this", confirm=True))
        exc_intent = [i for i in ctx.store.open_intents() if i["kind"] == "update"][0]
        for _ in range(8):   # eight companion ticks while the master's create waits for its backoff
            ctx.store.settle_intent(exc_intent["id"], "pending", {}, retry_in_sec=0)
            leased = ctx.store.lease_intent(exc_intent["id"], "companion", 90)
            ops.run_leased_intent(ctx.store, ctx.providers, leased)
        self.assertLessEqual(ctx.store.intents_for_event(exc_intent["event_id"])[0]["attempts"], 1)


class CopiesAndSplitTests(unittest.TestCase):
    def test_created_copies_never_carry_the_guest_list(self):
        ctx = make_context()
        full = add_local_calendar(ctx.store, "full", "full")
        created = json.loads(tools.cal_create(ctx, title="Встреча", start="2026-10-01T09:00+00:00", calendars=[DEFAULT_LOCAL_CALENDAR_ID, full],
                                              attendees=["alice@example.test"], confirm=True))
        members = ctx.store.group_masters(created["event"]["link_group_id"])
        copy = [m for m in members if m["calendar_id"] == full][0]
        self.assertEqual(json.loads(copy["attendees_json"]), [])
        primary = [m for m in members if m["calendar_id"] == DEFAULT_LOCAL_CALENDAR_ID][0]
        self.assertEqual(json.loads(primary["attendees_json"]), [{"email": "alice@example.test"}])

    def test_following_split_keeps_the_remaining_count(self):
        ctx = make_context()
        created = json.loads(tools.cal_create(ctx, title="Курс", start="2026-10-01T09:00+00:00", duration_min=60, rrule="FREQ=DAILY;COUNT=10", confirm=True))
        key = iso_utc(datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc))
        json.loads(tools.cal_update(ctx, id=f"{created['event']['id']}@{key}", start="2026-10-05T11:00+00:00", scope="following", confirm=True))
        rows = [r for r in ctx.store.window(datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc)) if r.get("rrule")]
        new = [r for r in rows if r["id"] != created["event"]["id"]][0]
        self.assertIn("COUNT=6", new["rrule"])
        occs = ctx.occurrences(datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(len(occs), 10)


class ChannelTests(unittest.TestCase):
    def test_token_rejected_keeps_reminders_without_burning_attempts(self):
        ctx = make_context()
        start = now_utc() + timedelta(minutes=10)
        json.loads(tools.cal_create(ctx, title="Скоро", start=start.isoformat(), duration_min=30, reminders=[15], confirm=True))

        class Rejected(rem.NotifyChannel):
            def state(self, refresh=False):
                return "token_rejected"
        for _ in range(6):
            rem.deliver_due(ctx.store, Rejected(), ctx.tz, now=now_utc())
        rows = ctx.store.upcoming_reminders(now_utc() - timedelta(hours=1), limit=10)
        self.assertEqual([r["state"] for r in rows], ["no_channel"])
        self.assertEqual(rows[0]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
