"""Public regressions for source deletion and recurrence slot identity."""

import asyncio
import json
import os
import sys
import unittest
from datetime import timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
for path in (os.path.dirname(HERE), HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import Request, RouteAPI, make_context
from test_calendar_provider_divergence import _day, caldav_context, sync
import routes
import tools
from model import get_tz, iso_utc


class RecurrenceIdentityTests(unittest.TestCase):
    def _series(self):
        ctx = make_context()
        first = _day(3)
        master = json.loads(tools.cal_create(ctx, title="Series", start=first.isoformat(),
                                             duration_min=30, rrule="FREQ=DAILY;COUNT=4", confirm=True))["event"]
        slot = first + timedelta(days=1)
        moved = slot + timedelta(hours=2)
        result = json.loads(tools.cal_update(ctx, id=f"{master['id']}@{iso_utc(slot)}",
                                             start=moved.isoformat(), scope="this", confirm=True))
        self.assertEqual(result["status"], "updated")
        return ctx, master["id"], first, slot, moved

    def test_duration_only_all_on_moved_occurrence_keeps_series_anchor(self):
        ctx, master_id, first, slot, moved = self._series()
        result = json.loads(tools.cal_update(ctx, id=f"{master_id}@{iso_utc(slot)}",
                                             duration_min=45, scope="all", confirm=True))
        self.assertEqual(result["status"], "updated")
        master = ctx.store.get_event(master_id)
        self.assertEqual(master["start_utc"], iso_utc(first))
        self.assertEqual(master["end_utc"], iso_utc(first + timedelta(minutes=45)))
        self.assertEqual(ctx.store.exceptions_for(master_id)[0]["recurrence_id"], iso_utc(slot))

    def test_end_only_http_all_on_moved_occurrence_keeps_series_anchor(self):
        ctx, master_id, first, slot, moved = self._series()
        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        response = asyncio.run(api.routes["event/update"](Request({"id": f"{master_id}@{iso_utc(slot)}",
                                                                  "end": (moved + timedelta(minutes=50)).isoformat(),
                                                                  "scope": "all"})))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ctx.store.get_event(master_id)["start_utc"], iso_utc(first))
        self.assertEqual(ctx.store.get_event(master_id)["end_utc"], iso_utc(first + timedelta(minutes=50)))

    def test_explicit_all_move_from_moved_occurrence_uses_effective_time(self):
        ctx, master_id, first, slot, moved = self._series()
        result = json.loads(tools.cal_update(ctx, id=f"{master_id}@{iso_utc(slot)}",
                                             start=(moved + timedelta(hours=1)).isoformat(),
                                             scope="all", confirm=True))
        self.assertEqual(result["status"], "updated")
        self.assertEqual(ctx.store.get_event(master_id)["start_utc"], iso_utc(first + timedelta(hours=1)))
        self.assertEqual(ctx.store.exceptions_for(master_id)[0]["recurrence_id"],
                         iso_utc(slot + timedelta(hours=1)))

    def test_offset_equivalent_id_reuses_exception_and_deletes_same_slot(self):
        ctx, master_id, first, slot, moved = self._series()
        offset_id = f"{master_id}@{slot.astimezone(timezone(timedelta(hours=3))).isoformat()}"
        changed = json.loads(tools.cal_update(ctx, id=offset_id, title="Changed", scope="this", confirm=True))
        self.assertEqual(changed["status"], "updated")
        self.assertEqual(len(ctx.store.exceptions_for(master_id)), 1)
        removed = json.loads(tools.cal_delete(ctx, id=offset_id, scope="this", confirm=True))
        self.assertEqual(removed["status"], "deleted")
        events = json.loads(tools.cal_events(ctx, start=slot.date().isoformat()))["events"]
        self.assertEqual(events, [])

    def test_invalid_and_nonexistent_slots_refused_before_any_write(self):
        ctx, master_id, first, slot, moved = self._series()
        before_master = ctx.store.get_event(master_id)
        before_exceptions = ctx.store.exceptions_for(master_id)
        for suffix in ("not-a-date", first.date().isoformat(), iso_utc(slot).replace("+00:00", ".500+00:00"),
                       iso_utc(first + timedelta(days=20))):
            event_id = f"{master_id}@{suffix}"
            self.assertNotEqual(json.loads(tools.cal_update(ctx, id=event_id, title="Wrong",
                                                           scope="this", confirm=True))["status"], "updated")
            self.assertNotEqual(json.loads(tools.cal_delete(ctx, id=event_id, scope="this",
                                                           confirm=True))["status"], "deleted")
            api = RouteAPI()
            routes.register_routes(api, lambda: ctx)
            response = asyncio.run(api.routes["event/delete"](Request({"id": event_id, "scope": "all"})))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(ctx.store.get_event(master_id), before_master)
        self.assertEqual(ctx.store.exceptions_for(master_id), before_exceptions)


class SourceDeletionTests(unittest.TestCase):
    def test_source_exdate_kept_override_cannot_be_read_or_updated_by_id(self):
        ctx, calendar_id, server = caldav_context()
        first = _day(3)
        master_id = json.loads(tools.cal_create(ctx, title="Source", start=first.isoformat(),
                                                duration_min=30, rrule="FREQ=DAILY;COUNT=4",
                                                calendars=[calendar_id], confirm=True))["event"]["id"]
        slot = first + timedelta(days=1)
        json.loads(tools.cal_update(ctx, id=f"{master_id}@{iso_utc(slot)}",
                                    start=(slot + timedelta(hours=2)).isoformat(), scope="this", confirm=True))
        exception_id = ctx.store.exceptions_for(master_id)[0]["id"]
        href = ctx.store.get_event(master_id)["href"]
        def cancel_slot(cal):
            master = next(c for c in cal.walk("VEVENT") if c.get("RECURRENCE-ID") is None)
            master.add("EXDATE", slot.astimezone(get_tz("UTC")))
        server.edit(href, cancel_slot)
        sync(ctx, calendar_id)
        self.assertEqual(json.loads(tools.cal_events(ctx, id=exception_id))["status"], "not_found")
        self.assertEqual(json.loads(tools.cal_events(ctx, id=f"{master_id}@{iso_utc(slot)}"))["status"], "not_found")
        self.assertEqual(json.loads(tools.cal_update(ctx, id=exception_id, title="Resurrect",
                                                     scope="this", confirm=True))["status"], "error")
        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        response = asyncio.run(api.routes["event/update"](Request({"id": exception_id, "title": "Resurrect"})))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(tools.cal_events(ctx, start=slot.date().isoformat()))["events"], [])

    def test_provider_deletes_unlinked_master_with_moved_exception(self):
        ctx, calendar_id, server = caldav_context()
        first = _day(3)
        master_id = json.loads(tools.cal_create(ctx, title="Source", start=first.isoformat(),
                                                duration_min=30, rrule="FREQ=DAILY;COUNT=4",
                                                calendars=[calendar_id], confirm=True))["event"]["id"]
        slot = first + timedelta(days=1)
        moved = slot + timedelta(hours=2)
        json.loads(tools.cal_update(ctx, id=f"{master_id}@{iso_utc(slot)}", start=moved.isoformat(),
                                    scope="this", confirm=True))
        href = ctx.store.get_event(master_id)["href"]
        self.assertEqual(len(ctx.store.exceptions_for(master_id)), 1)
        del server.resources[href]
        sync(ctx, calendar_id)
        self.assertEqual(json.loads(tools.cal_events(ctx, start=slot.date().isoformat()))["events"], [])
        self.assertEqual(ctx.store.exceptions_for(master_id), [])
