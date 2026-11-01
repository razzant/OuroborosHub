"""Availability and attendee clearing through real tools/adapters, offline servers."""
import json
import shutil
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch

from test_calendar_provider_divergence import caldav_context, google_context, sync, _day
from test_calendar_round2 import make_context
import tools
from model import get_tz, DEFAULT_LOCAL_CALENDAR_ID
from providers import ics_to_rows, row_to_ics, _ical


class AvailabilityTests(unittest.TestCase):
    def keep(self, ctx):
        self.addCleanup(shutil.rmtree, ctx.state_dir)
        ctx.tz = get_tz("UTC")
        return ctx

    def free(self, ctx, start, **kwargs):
        return json.loads(tools.cal_free(ctx, start=start.isoformat(),
                         end=(start + timedelta(hours=3)).isoformat(),
                         working_hours="00:00-23:59", duration_min=30, **kwargs))

    def test_disabled_busy_sources_are_empty_but_explicit_source_still_works(self):
        ctx = self.keep(make_context())
        start = _day(3)
        for title, offset, availability in (("Busy", 1, "busy"), ("Usual", 0, "soft")):
            result = json.loads(tools.cal_create(ctx, title=title,
                start=(start + timedelta(hours=offset)).isoformat(),
                duration_min=60, availability=availability, confirm=True))
            self.assertEqual(result["status"], "created")
        for calendar in ctx.store.list_calendars():
            tools.cal_settings(ctx, action="set_calendar", calendar_id=calendar["id"],
                               busy_source=False, confirm=True)
        with patch.object(ctx, "occurrences", wraps=ctx.occurrences) as reader:
            free = self.free(ctx, start)
        self.assertEqual(free["sources"], [])
        self.assertEqual(free["soft_overlaps"], [])
        self.assertEqual(len(free["slots"]), 1)
        self.assertEqual(free["slots"][0]["start"], start.isoformat())
        self.assertEqual(free["slots"][0]["end"], (start + timedelta(hours=3)).isoformat())
        reader.assert_not_called()
        explicit = self.free(ctx, start, sources=[DEFAULT_LOCAL_CALENDAR_ID])
        self.assertEqual(len(explicit["slots"]), 2)
        self.assertTrue(explicit["soft_overlaps"])

    def test_google_clear_attendees_survives_sync_and_honors_send_updates(self):
        for send_updates in (False, True):
            with self.subTest(send_updates=send_updates):
                ctx, calendar_id, server = google_context()
                self.keep(ctx)
                created = json.loads(tools.cal_create(ctx, title="Guests", start=_day(4).isoformat(),
                    calendars=[calendar_id], attendees=[{"email": "alice@example.test"}], confirm=True))
                event_id = created["event"]["id"]
                renamed = json.loads(tools.cal_update(ctx, id=event_id, title="Same guests", confirm=True))
                self.assertEqual(renamed["status"], "updated")
                self.assertEqual(server.live()[0]["attendees"][0]["email"], "alice@example.test")
                cleared = json.loads(tools.cal_update(ctx, id=event_id, attendees=[],
                                      send_updates=send_updates, confirm=True))
                self.assertEqual(cleared["status"], "updated")
                sent = [call for call in server.calls if call[0] == "PATCH"][-1]
                self.assertEqual(sent[3]["attendees"], [])
                self.assertEqual(sent[2]["sendUpdates"], "all" if send_updates else "none")
                self.assertEqual(server.live()[0]["attendees"], [])
                sync(ctx, calendar_id)
                self.assertEqual(json.loads(ctx.store.get_event(event_id)["attendees_json"]), [])

    def test_google_unrelated_edit_and_rsvp_preserve_guest_metadata(self):
        ctx, calendar_id, server = google_context()
        self.keep(ctx)
        created = json.loads(tools.cal_create(ctx, title="Roundtrip", start=_day(4).isoformat(),
            calendars=[calendar_id], attendees=[{"email":"guest@example.test"}], confirm=True))
        row_id = created["event"]["id"]
        remote = server.live()[0]
        guests = [{"email":"guest@example.test", "displayName":"Guest", "optional":True,
                   "comment":"Arrive late", "additionalGuests":2, "responseStatus":"tentative"},
                  {"email":"owner@example.test", "self":True, "responseStatus":"needsAction"}]
        remote["attendees"] = guests
        remote["etag"] = server._etag()
        sync(ctx, calendar_id)
        changed = json.loads(tools.cal_update(ctx, id=row_id, title="Rename only", confirm=True))
        self.assertEqual(changed["status"], "updated")
        self.assertEqual(remote["attendees"], guests)
        self.assertNotIn("attendees", [c for c in server.calls if c[0] == "PATCH"][-1][3])
        answered = json.loads(tools.cal_update(ctx, id=row_id, response="accepted", confirm=True))
        self.assertEqual(answered["status"], "updated")
        self.assertEqual(remote["attendees"][0], guests[0])
        self.assertEqual(remote["attendees"][1]["responseStatus"], "accepted")

    def ics(self, transp="TRANSPARENT"):
        start = _day(5)
        prop = "" if transp is None else f"TRANSP:{transp}\r\n"
        return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nBEGIN:VEVENT\r\n"
                "UID:availability-test\r\nSUMMARY:Available\r\nX-SOURCE:retained\r\n"
                f"DTSTART:{start:%Y%m%dT%H%M%SZ}\r\n"
                f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%SZ}\r\n"
                f"{prop}END:VEVENT\r\nEND:VCALENDAR\r\n")

    def test_ics_read_transparency_and_default(self):
        for value, expected in (("TRANSPARENT", "free"), ("OPAQUE", "busy"), (None, "busy")):
            with self.subTest(value=value):
                row = ics_to_rows(self.ics(value), "cal", "href", "etag", timezone.utc)[0]
                self.assertEqual(row["availability"], expected)

    def test_new_free_event_writes_transparent(self):
        row = ics_to_rows(self.ics(None), "cal", "href", "etag", timezone.utc)[0]
        row.update(raw_payload="", availability="free")
        event = _ical().Calendar.from_ical(row_to_ics(row)).walk("VEVENT")[0]
        self.assertEqual(str(event["TRANSP"]), "TRANSPARENT")

    def test_raw_ics_replaces_transparency_and_keeps_unknown_fields(self):
        row = ics_to_rows(self.ics(), "cal", "href", "etag", timezone.utc)[0]
        for availability, expected in (("busy", "OPAQUE"), ("free", "TRANSPARENT")):
            row["availability"] = availability
            text = row_to_ics(row)
            event = _ical().Calendar.from_ical(text).walk("VEVENT")[0]
            self.assertEqual(str(event["TRANSP"]), expected)
            self.assertEqual(text.count("TRANSP:"), 1)
            self.assertEqual(str(event["X-SOURCE"]), "retained")
            self.assertEqual(str(event["UID"]), "availability-test")
            row["raw_payload"] = text

    def test_caldav_import_update_roundtrip_changes_free_windows(self):
        ctx, calendar_id, server = caldav_context()
        self.keep(ctx)
        href = ctx.store.get_calendar(calendar_id)["href"] + "availability-test.ics"
        server.resources[href] = (self.ics(), '"source1"')
        sync(ctx, calendar_id)
        start = _day(5)
        row = ctx.store.window(start, start + timedelta(hours=3), [calendar_id])[0]
        self.assertEqual(row["availability"], "free")
        self.assertEqual(self.free(ctx, start, sources=[calendar_id])["slots"][0]["start"], start.isoformat())
        for availability, expected in (("busy", "OPAQUE"), ("free", "TRANSPARENT")):
            out = json.loads(tools.cal_update(ctx, id=row["id"], availability=availability, confirm=True))
            self.assertEqual(out["status"], "updated")
            event = _ical().Calendar.from_ical(server.resources[href][0]).walk("VEVENT")[0]
            self.assertEqual(str(event["TRANSP"]), expected)
            sync(ctx, calendar_id)
            self.assertEqual(ctx.store.get_event(row["id"])["availability"], availability)
            expected_start = start + timedelta(hours=1) if availability == "busy" else start
            self.assertEqual(self.free(ctx, start, sources=[calendar_id])["slots"][0]["start"], expected_start.isoformat())


if __name__ == "__main__":
    unittest.main()
