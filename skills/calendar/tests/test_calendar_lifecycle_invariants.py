"""Public-command regressions for scoped deletion and provider-write custody.

No personal calendars: every provider is an in-memory recording adapter and every
store is a temporary SQLite database.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
for path in (SKILL, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import (  # noqa: E402
    OneAdapterProviders, RecordingAdapter, Request, RouteAPI,
    add_external_calendar, add_local_calendar, make_context,
)
import ops  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
from model import iso_utc  # noqa: E402


class LifecycleInvariants(unittest.TestCase):
    def test_invalid_scope_and_master_this_cannot_mutate_through_any_door(self):
        ctx = make_context()
        master = json.loads(tools.cal_create(ctx, title="Series", start="2026-10-01T09:00+00:00",
                                            rrule="FREQ=DAILY;COUNT=4", confirm=True))["event"]
        before = ctx.store.get_event(master["id"])
        for scope in ("al", "this", "following"):
            reply = json.loads(tools.cal_delete(ctx, id=master["id"], scope=scope, confirm=True))
            self.assertIn(reply["status"], ("error", "ambiguous"))
            self.assertEqual(ctx.store.get_event(master["id"]), before)
        with self.assertRaises(ValueError):
            ops.delete_event(ctx.store, ctx.providers, master["id"], scope="al")
        with self.assertRaises(ValueError):
            ops.delete_event(ctx.store, ctx.providers, master["id"], scope="this")
        self.assertEqual(ctx.store.get_event(master["id"]), before)
        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        for scope in ("al", "this", "following"):
            answer = asyncio.run(api.routes["event/delete"](Request({"id": master["id"], "scope": scope})))
            self.assertEqual(answer.status_code, 400)
            self.assertEqual(ctx.store.get_event(master["id"]), before)

    def test_definitely_rejected_first_create_can_be_cancelled_locally(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)

        class RejectCreate(RecordingAdapter):
            def create(self, calendar, event, payload):
                raise ops.ProviderError("unsupported", "remote rejected create")

        adapter = RejectCreate()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Rejected", start="2026-10-01T09:00+00:00",
                                              calendars=[external], confirm=True))["event"]
        result = json.loads(tools.cal_delete(ctx, id=created["id"], scope="all", confirm=True))
        self.assertEqual(result["status"], "deleted")
        self.assertIsNone(ctx.store.get_event(created["id"]))
        self.assertFalse(any(call[0] == "delete" for call in adapter.calls))

    def test_unaddressed_pending_create_cannot_return_deleted_then_reappear(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        # Simulate a provider unavailable before the first create: the owner
        # cancellation must not drop the row while its create intent is alive.
        ctx.providers = OneAdapterProviders(None)
        created = json.loads(tools.cal_create(ctx, title="Cancel while pending", start="2026-10-01T09:00+00:00",
                                              calendars=[external], confirm=True))
        event_id = created["event"]["id"]
        self.assertFalse(ctx.store.get_event(event_id)["external_id"])
        cancelled = json.loads(tools.cal_delete(ctx, id=event_id, scope="all", confirm=True))
        self.assertEqual(cancelled["status"], "pending")
        self.assertEqual(ctx.store.get_event(event_id)["sync_state"], "pending_delete")
        ctx.providers = OneAdapterProviders(adapter)
        intents = ctx.store.intents_for_event(event_id)
        self.assertEqual([i["kind"] for i in intents], ["create", "delete"])
        ops.execute_intent(ctx.store, ctx.providers, intents[0], "test")
        self.assertEqual(ctx.store.get_event(event_id)["sync_state"], "pending_delete")
        ops.execute_intent(ctx.store, ctx.providers, intents[1], "test")
        self.assertIsNone(ctx.store.get_event(event_id))
        self.assertEqual([call[0] for call in adapter.calls], ["create", "delete"])

    def test_confirmed_missing_copy_after_failed_delete_never_cascades(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Linked", start="2026-10-01T09:00+00:00",
                                              calendars=["local:personal", external], confirm=True))
        primary = created["event"]
        copy = next(m for m in ctx.store.group_masters(primary["link_group_id"]) if m["id"] != primary["id"])
        adapter.fail_delete = "forbidden"
        result = ops.reassign_event(ctx.store, ctx.providers, ctx.store.get_event(primary["id"]), ["local:personal"])
        self.assertEqual(result["removed"][0]["assignments"][0]["status"], "failed")
        from scripts.worker import _confirmed_deletion
        self.assertEqual(_confirmed_deletion(ctx.store, ctx.providers, ctx.store.get_event(copy["id"])), 1)
        self.assertIsNone(ctx.store.get_event(copy["id"]))
        self.assertIsNotNone(ctx.store.get_event(primary["id"]))

    def test_all_day_following_delete_still_expands_without_naive_until(self):
        ctx = make_context()
        master = json.loads(tools.cal_create(ctx, title="Days", start="2026-10-01",
                                            all_day=True, rrule="FREQ=DAILY;COUNT=5", confirm=True))["event"]
        occurrence = json.loads(tools.cal_events(ctx, start="2026-10-03", end="2026-10-04"))["events"][0]["id"]
        deleted = json.loads(tools.cal_delete(ctx, id=occurrence, scope="following", confirm=True))
        self.assertEqual(deleted["status"], "deleted")
        first = datetime(2026, 10, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 6, tzinfo=timezone.utc)
        expanded = ops.expand(ctx.store.window(first, end), first, end, ctx.store.exceptions_for, strict=True)
        self.assertEqual(len(expanded), 2)
        self.assertTrue(all(e["id"].startswith(master["id"]) for e in expanded))

    def test_following_delete_truncates_explicit_rdates_too(self):
        ctx = make_context()
        master = json.loads(tools.cal_create(ctx, title="Extra", start="2026-10-01T09:00+00:00",
                                            rrule="FREQ=DAILY;COUNT=5", confirm=True))["event"]
        ctx.store.update_event(master["id"], {"rdates": "2026-10-07T09:00:00+00:00"})
        out = json.loads(tools.cal_delete(ctx, id=master["id"] + "@2026-10-03T09:00:00+00:00",
                                          scope="following", confirm=True))
        self.assertEqual(out["status"], "deleted")
        self.assertEqual(ctx.store.get_event(master["id"])["rdates"], "")
        occurrences = json.loads(tools.cal_events(ctx, start="2026-10-07", end="2026-10-08"))["events"]
        self.assertFalse(occurrences)

    def test_provider_recurring_rule_and_title_changes_propagate_without_time_move(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        local = add_local_calendar(ctx.store, "copy", publish_mode="full")
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Source", start="2026-10-01T09:00+00:00",
                                              rrule="FREQ=DAILY;COUNT=5", calendars=[external, local], confirm=True))
        primary = created["event"]
        copy = next(m for m in ctx.store.group_masters(primary["link_group_id"]) if m["id"] != primary["id"])
        source = ctx.store.get_event(primary["id"])
        remote = {**source, "etag": '"remote-new"', "title": "Renamed upstream", "rrule": "FREQ=WEEKLY;COUNT=5"}

        class Fetch:
            def fetch(self, calendar, cursor, tz):
                return [remote], "", "window", [source["href"]]

        from scripts.worker import reconcile_calendar
        report = reconcile_calendar(ctx.store, ctx.providers, Fetch(), ctx.store.get_calendar(external), ctx.tz)
        self.assertGreater(report[2], 0)
        changed = ctx.store.get_event(copy["id"])
        self.assertEqual((changed["title"], changed["rrule"]), ("Renamed upstream", "FREQ=WEEKLY;COUNT=5"))

    def test_late_sync_writer_cannot_erase_pending_delete(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        ctx.providers = OneAdapterProviders(None)
        created = json.loads(tools.cal_create(ctx, title="Delete race", start="2026-10-01T09:00+00:00",
                                              calendars=[external], confirm=True))["event"]
        json.loads(tools.cal_delete(ctx, id=created["id"], scope="all", confirm=True))
        ctx.store.update_event(created["id"], {"sync_state": "synced", "etag": '"late"'})
        self.assertEqual(ctx.store.get_event(created["id"])["sync_state"], "pending_delete")
        # Provider absence cannot close deletion while a create may still be in flight.
        self.assertFalse(ctx.store.confirm_pending_delete(created["id"]))
        self.assertIsNotNone(ctx.store.get_event(created["id"]))

    def test_provider_conflict_then_confirmed_absence_closes_deletion(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Remove", start="2026-10-01T09:00+00:00",
                                              calendars=[external], confirm=True))["event"]
        adapter.fail_delete = "conflict"
        out = json.loads(tools.cal_delete(ctx, id=created["id"], scope="all", confirm=True))
        self.assertEqual(out["status"], "conflict")
        self.assertEqual(ctx.store.get_event(created["id"])["sync_state"], "conflict")
        self.assertTrue(ctx.store.delete_requested(created["id"]))
        from scripts.worker import _confirmed_deletion
        self.assertEqual(_confirmed_deletion(ctx.store, ctx.providers, ctx.store.get_event(created["id"])), 1)
        self.assertIsNone(ctx.store.get_event(created["id"]))

    def test_reassign_cannot_resurrect_a_copy_awaiting_remote_delete(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Linked", start="2026-10-01T09:00+00:00",
                                              calendars=["local:personal", external], confirm=True))
        primary = created["event"]
        adapter.fail_delete = "network"
        ops.reassign_event(ctx.store, ctx.providers, ctx.store.get_event(primary["id"]), ["local:personal"])
        ops.update_event(ctx.store, ctx.providers, primary["id"], {"title": "Edited primary"}, scope="all")
        out = ops.reassign_event(ctx.store, ctx.providers, ctx.store.get_event(primary["id"]),
                                 ["local:personal", external])
        self.assertEqual(out["failed"], "pending_delete")
        self.assertEqual(out["added"][0]["status"], "conflict")
        self.assertEqual(len([call for call in adapter.calls if call[0] == "create"]), 1)

    def test_following_title_edit_keeps_finite_end_and_original_slots(self):
        ctx = make_context()
        master = json.loads(tools.cal_create(ctx, title="Original", start="2026-10-01T09:00+00:00",
                                            rrule="FREQ=DAILY;UNTIL=20261005T090000Z", confirm=True))["event"]
        moved = master["id"] + "@2026-10-03T09:00:00+00:00"
        tools.cal_update(ctx, id=moved, start="2026-10-03T11:00+00:00", scope="this", confirm=True)
        result = json.loads(tools.cal_update(ctx, id=moved, title="Renamed", scope="following", confirm=True))
        self.assertEqual(result["status"], "updated")
        new_master = ctx.store.get_event(result["event"]["id"]) if result["event"]["id"] != master["id"] else None
        if new_master is None:
            new_master = next(e for e in ctx.store.window(datetime(2026, 10, 1, tzinfo=timezone.utc),
                                                         datetime(2026, 10, 8, tzinfo=timezone.utc))
                              if e.get("rrule") and e["id"] != master["id"])
        self.assertEqual(new_master["start_utc"], "2026-10-03T09:00:00+00:00")
        self.assertIn("UNTIL=20261005T090000Z", new_master["rrule"])
        self.assertEqual(new_master["title"], "Renamed")
        exception = next(e for e in ctx.store.exceptions_for(new_master["id"]) if e["recurrence_id"] == iso_utc(datetime(2026, 10, 3, 9, tzinfo=timezone.utc)))
        self.assertEqual(exception["start_utc"], "2026-10-03T11:00:00+00:00")

    def test_create_answer_arriving_after_delete_keeps_delete_queued(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)

        class DeleteDuringCreate(RecordingAdapter):
            def create(self, calendar, event, payload):
                deletion = ops.delete_event(ctx.store, ctx.providers, event["id"], scope="all")
                self.assert_pending = deletion["assignments"][0]["status"]
                return super().create(calendar, event, payload)

        adapter = DeleteDuringCreate()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Race", start="2026-10-01T09:00+00:00",
                                              calendars=[external], confirm=True))
        event_id = created["event"]["id"]
        self.assertEqual(adapter.assert_pending, "pending")
        self.assertEqual(ctx.store.get_event(event_id)["sync_state"], "pending_delete")
        self.assertEqual([i["state"] for i in ctx.store.intents_for_event(event_id)], ["done", "pending"])
        ops.execute_intent(ctx.store, ctx.providers, ctx.store.intents_for_event(event_id)[1], "test")
        self.assertIsNone(ctx.store.get_event(event_id))
        self.assertEqual([call[0] for call in adapter.calls], ["create", "delete"])

    def test_cancel_occurrence_while_master_create_pending_reaches_provider(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        ctx.providers = OneAdapterProviders(None)
        created = json.loads(tools.cal_create(ctx, title="Series pending", start="2026-10-01T09:00+00:00",
                                              rrule="FREQ=DAILY;COUNT=4", calendars=[external], confirm=True))
        master_id = created["event"]["id"]
        occurrence = master_id + "@2026-10-02T09:00:00+00:00"
        out = json.loads(tools.cal_delete(ctx, id=occurrence, scope="this", confirm=True))
        self.assertEqual(out["status"], "pending")
        exception = ctx.store.exceptions_for(master_id)[0]
        self.assertEqual(exception["status"], "cancelled")
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        ops.execute_intent(ctx.store, ctx.providers, ctx.store.intents_for_event(master_id)[0], "test")
        ops.execute_intent(ctx.store, ctx.providers, ctx.store.intents_for_event(exception["id"])[0], "test")
        updates = [call for call in adapter.calls if call[0] == "update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][2]["status"], "cancelled")
        self.assertEqual(updates[0][2]["master_external_id"], "remote-" + master_id)

    def test_invalid_reassignment_calendar_does_not_commit_the_other_changes(self):
        ctx = make_context()
        created = json.loads(tools.cal_create(ctx, title="Original", start="2026-10-01T09:00+00:00",
                                              confirm=True))["event"]
        before = ctx.store.get_event(created["id"])
        out = json.loads(tools.cal_update(ctx, id=created["id"], title="Should not save",
                                          calendars=["missing"], confirm=True))
        self.assertEqual(out["status"], "error")
        self.assertEqual(ctx.store.get_event(created["id"]), before)
        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        reply = asyncio.run(api.routes["event/update"](Request({"id": created["id"], "title": "Wrong",
                                                                "calendars": ["missing"]})))
        self.assertEqual(reply.status_code, 400)
        self.assertEqual(ctx.store.get_event(created["id"]), before)


if __name__ == "__main__":
    unittest.main()
