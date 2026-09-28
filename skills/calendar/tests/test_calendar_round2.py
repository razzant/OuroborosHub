"""Round 2 adversarial QA for the calendar skill.

The assertions describe the frozen product contract.  A failing test is a
reproducible review finding; failures are intentionally not marked expected.
All state is an isolated SQLite database under a temporary directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

try:
    import starlette.responses  # noqa: F401
except ModuleNotFoundError:
    starlette = types.ModuleType("starlette")
    responses = types.ModuleType("starlette.responses")

    class JSONResponse:
        def __init__(self, content, status_code=200):
            self.body = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.status_code = status_code

    responses.JSONResponse = JSONResponse
    responses.HTMLResponse = JSONResponse
    starlette.responses = responses
    sys.modules["starlette"] = starlette
    sys.modules["starlette.responses"] = responses

import ops  # noqa: E402
import reminders as rem  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
from model import BUSY_COPY_TITLE, DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc  # noqa: E402
from providers import Providers as RealProviders, YandexAdapter, ics_to_rows  # noqa: E402
from providers_google import GoogleAdapter  # noqa: E402
from store import Store  # noqa: E402


class FakeAPI:
    def __init__(self, secrets=None):
        self.state_dir = tempfile.mkdtemp(prefix="calendar-round2-")
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
        self.fail_delete = ""

    def create(self, calendar, event, payload):
        self.calls.append(("create", calendar["id"], event["id"], dict(payload)))
        return {
            "external_id": "remote-" + event["id"],
            "href": "https://example.test/" + event["id"] + ".ics",
            "etag": '"e1"',
        }

    def update(self, calendar, event, expected_etag, payload):
        self.calls.append(("update", calendar["id"], dict(event), expected_etag, dict(payload)))
        return {"etag": '"e2"', "external_id": event.get("external_id") or "instance-id"}

    def delete(self, calendar, event, expected_etag, payload=None):
        self.calls.append(("delete", calendar["id"], event["id"], expected_etag, dict(payload or {})))
        if self.fail_delete:
            kind, self.fail_delete = self.fail_delete, ""
            raise ops.ProviderError(kind, "simulated delete failure")

    def respond(self, calendar, event, payload):
        self.calls.append(("respond", calendar["id"], event["id"], dict(payload)))
        return {"etag": '"r2"'}


class OneAdapterProviders:
    def __init__(self, adapter):
        self.adapter = adapter

    def adapter_for(self, _account_id):
        return self.adapter


class Channel:
    def __init__(self, result=("sent", "accepted")):
        self.result = result
        self.sent = []

    def send(self, notice_id, text):
        self.sent.append((notice_id, text))
        return self.result


class RouteAPI:
    def __init__(self):
        self.routes = {}

    def register_route(self, name, handler, methods):
        self.routes[name] = handler


class Request:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query_params = query or {}

    async def json(self):
        return self._body


def make_context():
    return tools.Context(FakeAPI())


def add_local_calendar(store, suffix, publish_mode="full"):
    calendar_id = "local:" + suffix
    store.upsert_calendar(
        {
            "id": calendar_id,
            "account_id": "local",
            "provider": "local",
            "external_id": suffix,
            "name": suffix,
            "writable": True,
            "publish_mode": publish_mode,
        }
    )
    return calendar_id


def add_external_calendar(store, suffix="work", publish_mode="full"):
    account = "yandex:round2@example.test"
    store.upsert_account(
        {
            "id": account,
            "provider": "yandex",
            "alias": "round2",
            "login": "round2@example.test",
            "status": "ok",
        }
    )
    calendar_id = f"{account}:{suffix}"
    store.upsert_calendar(
        {
            "id": calendar_id,
            "account_id": account,
            "provider": "yandex",
            "external_id": suffix,
            "href": f"https://caldav.yandex.ru/calendars/round2/{suffix}/",
            "name": suffix,
            "writable": True,
            "publish_mode": publish_mode,
        }
    )
    return calendar_id


def create_linked_series(ctx, calendar_ids):
    return json.loads(
        tools.cal_create(
            ctx,
            title="Linked series",
            start="2026-10-01T09:00+00:00",
            duration_min=60,
            rrule="FREQ=DAILY",
            calendars=calendar_ids,
            confirm=True,
        )
    )


class ReassignAndLinkedSeriesRound2Tests(unittest.TestCase):
    def test_reassign_removing_one_copy_keeps_primary_and_other_copy(self):
        ctx = make_context()
        full = add_local_calendar(ctx.store, "full", "full")
        busy = add_local_calendar(ctx.store, "busy", "busy")
        created = json.loads(
            tools.cal_create(
                ctx,
                title="Keep me",
                start="2026-10-01T09:00+00:00",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, full, busy],
                confirm=True,
            )
        )
        primary_id = created["event"]["id"]

        ops.reassign_event(
            ctx.store,
            ctx.providers,
            ctx.store.get_event(primary_id),
            [DEFAULT_LOCAL_CALENDAR_ID, full],
        )

        self.assertIsNone(ctx.store.get_event(primary_id)["deleted_at"])
        members = ctx.store.group_members(created["event"]["link_group_id"])
        self.assertEqual({m["calendar_id"] for m in members}, {DEFAULT_LOCAL_CALENDAR_ID, full})

    def test_reassign_clicked_copy_still_keeps_the_primary(self):
        ctx = make_context()
        full = add_local_calendar(ctx.store, "full", "full")
        created = json.loads(
            tools.cal_create(
                ctx,
                title="Clicked copy",
                start="2026-10-01T09:00+00:00",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, full],
                confirm=True,
            )
        )
        members = ctx.store.group_members(created["event"]["link_group_id"])
        copy = next(m for m in members if not m["is_primary"])
        result = ops.reassign_event(ctx.store, ctx.providers, copy, [DEFAULT_LOCAL_CALENDAR_ID, full])
        self.assertEqual(result["kept_primary"], DEFAULT_LOCAL_CALENDAR_ID)
        self.assertIsNone(ctx.store.get_event(created["event"]["id"])["deleted_at"])

    def test_reassign_full_copy_preserves_full_content(self):
        ctx = make_context()
        full = add_local_calendar(ctx.store, "full", "full")
        created = json.loads(
            tools.cal_create(
                ctx,
                title="Full details",
                start="2026-10-01T09:00+00:00",
                description="description",
                location="location",
                attendees=["alice@example.test"],
                reminders=[15],
                confirm=True,
            )
        )
        source = ctx.store.get_event(created["event"]["id"])
        ops.reassign_event(ctx.store, ctx.providers, source, [DEFAULT_LOCAL_CALENDAR_ID, full])
        copy = next(m for m in ctx.store.group_members(ctx.store.get_event(source["id"])["link_group_id"]) if m["calendar_id"] == full)
        self.assertEqual((copy["title"], copy["description"], copy["location"]), ("Full details", "description", "location"))
        # owner decision (plan «служебные отражения не дублируют список гостей»): a copy never re-invites the attendees
        self.assertEqual(json.loads(copy["attendees_json"]), [])
        self.assertEqual(json.loads(copy["reminders_json"]), [])

    def test_this_scope_creates_one_exception_per_linked_master_not_nested_exceptions(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "second", "full")
        created = create_linked_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        occurrence = json.loads(
            tools.cal_events(ctx, start="2026-10-02", end="2026-10-03")
        )["events"][0]
        json.loads(
            tools.cal_update(
                ctx,
                id=occurrence["id"],
                start="2026-10-02T11:00+00:00",
                scope="this",
                confirm=True,
            )
        )
        all_members = ctx.store.group_members(created["event"]["link_group_id"])
        masters = {m["id"] for m in all_members if not m["master_id"]}
        exceptions = [m for m in all_members if m["master_id"]]
        self.assertEqual(len(exceptions), 2)
        self.assertTrue(all(row["master_id"] in masters for row in exceptions))

    def test_following_split_new_linked_masters_share_uid_and_link_group(self):
        ctx = make_context()
        second = add_local_calendar(ctx.store, "second", "full")
        create_linked_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        occurrence = json.loads(
            tools.cal_events(ctx, start="2026-10-03", end="2026-10-04")
        )["events"][0]
        json.loads(
            tools.cal_update(
                ctx,
                id=occurrence["id"],
                start="2026-10-03T10:00+00:00",
                scope="following",
                confirm=True,
            )
        )
        future = json.loads(
            tools.cal_events(ctx, start="2026-10-05", end="2026-10-06")
        )["events"]
        self.assertEqual(len({event["link_group_id"] for event in future}), 1)
        new_group = future[0]["link_group_id"]
        new_masters = [m for m in ctx.store.group_members(new_group) if not m["master_id"]]
        self.assertEqual(len({m["uid"] for m in new_masters}), 1)

    def test_following_split_of_linked_series_moves_late_exceptions_once(self):
        ctx = make_context()
        ctx.tz = get_tz("Asia/Dubai")  # The expected 16:00 is 12:00 UTC in this fixed zone.
        second = add_local_calendar(ctx.store, "second", "full")
        create_linked_series(ctx, [DEFAULT_LOCAL_CALENDAR_ID, second])
        week = json.loads(
            tools.cal_events(ctx, start="2026-10-01", end="2026-10-08")
        )["events"]
        late = next(event for event in week if event["start"][:10] == "2026-10-06")
        json.loads(
            tools.cal_update(
                ctx,
                id=late["id"],
                start="2026-10-06T12:00+00:00",
                scope="this",
                confirm=True,
            )
        )
        split = next(event for event in week if event["start"][:10] == "2026-10-03")
        json.loads(
            tools.cal_update(
                ctx,
                id=split["id"],
                start="2026-10-03T10:00+00:00",
                scope="following",
                confirm=True,
            )
        )
        future = json.loads(
            tools.cal_events(ctx, start="2026-10-06", end="2026-10-07")
        )["events"]
        self.assertEqual(len(future), 2)
        self.assertEqual({event["start"][11:16] for event in future}, {"16:00"})
        self.assertEqual(len({event["link_group_id"] for event in future}), 1)

    def test_reassign_reports_pending_removed_calendar(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        adapter = RecordingAdapter()
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(
            tools.cal_create(
                ctx,
                title="Removal status",
                start="2026-10-01T09:00+00:00",
                calendars=[DEFAULT_LOCAL_CALENDAR_ID, external],
                confirm=True,
            )
        )
        adapter.fail_delete = "network"
        result = json.loads(
            tools.cal_update(
                ctx,
                id=created["event"]["id"],
                calendars=[DEFAULT_LOCAL_CALENDAR_ID],
                confirm=True,
            )
        )
        self.assertEqual(result["status"], "pending")
        removed = result["reassign"]["removed"][0]["assignments"]
        self.assertIn("pending", {assignment["status"] for assignment in removed})

    def test_provider_confirmation_of_removed_copy_does_not_delete_primary(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        adapter = RecordingAdapter()
        adapter.fail_delete = "network"
        ctx.providers = OneAdapterProviders(adapter)
        created = json.loads(tools.cal_create(ctx, title="Primary survives", start="2026-10-01T09:00+00:00",
                                              calendars=[DEFAULT_LOCAL_CALENDAR_ID, external], confirm=True))
        primary = created["event"]
        copy = next(m for m in ctx.store.group_masters(primary["link_group_id"]) if m["id"] != primary["id"])
        removed = ops.reassign_event(ctx.store, ctx.providers, ctx.store.get_event(primary["id"]), [DEFAULT_LOCAL_CALENDAR_ID])
        self.assertEqual(removed["removed"][0]["assignments"][0]["status"], "pending")
        self.assertEqual(ctx.store.get_event(copy["id"])["sync_state"], "pending_delete")
        from scripts.worker import _confirmed_deletion
        _confirmed_deletion(ctx.store, ctx.providers, ctx.store.get_event(copy["id"]))
        self.assertIsNone(ctx.store.get_event(copy["id"]))
        self.assertIsNotNone(ctx.store.get_event(primary["id"]))
        self.assertFalse(any(i["state"] == "pending" for i in ctx.store.intents_for_event(copy["id"])))

    def test_linked_copy_delete_preview_names_every_target_of_confirmation(self):
        ctx = make_context()
        external = add_external_calendar(ctx.store)
        ctx.providers = OneAdapterProviders(RecordingAdapter())
        created = json.loads(tools.cal_create(ctx, title="Linked", start="2026-10-01T09:00+00:00",
                                              calendars=[DEFAULT_LOCAL_CALENDAR_ID, external], confirm=True))
        primary = created["event"]
        copy = next(m for m in ctx.store.group_masters(primary["link_group_id"]) if m["id"] != primary["id"])
        preview = json.loads(tools.cal_delete(ctx, id=copy["id"], scope="all", confirm=False))
        self.assertEqual({m["event_id"] for m in preview["affected"]}, {primary["id"], copy["id"]})
        self.assertIsNotNone(ctx.store.get_event(primary["id"]))


class ProviderIdentityRound2Tests(unittest.TestCase):
    def google_adapter(self):
        adapter = GoogleAdapter(
            "owner@example.test",
            "token",
            "refresh",
            datetime.now(timezone.utc) + timedelta(days=1),
            "client",
            "",
            None,
        )
        adapter.requests = []

        def request(method, path, params=None, body=None, headers=None, _retry=True):
            adapter.requests.append((method, path, params, body, headers))
            return 200, {}, {"id": path.rsplit("/", 1)[-1]}

        adapter._request = request
        return adapter

    def test_google_timed_instance_id_uses_master_and_recurrence_key(self):
        adapter = self.google_adapter()
        event = {
            "master_id": "local-master",
            "master_external_id": "google-master",
            "recurrence_id": "2026-10-01T09:00:00+00:00",
            "all_day": 0,
        }
        instance = adapter._instance_id({"external_id": "primary"}, event)
        self.assertEqual(instance, "google-master_20261001T090000Z")

    def test_google_all_day_instance_id_uses_event_timezone_date(self):
        adapter = self.google_adapter()
        event = {
            "master_id": "local-master",
            "master_external_id": "google-master",
            "recurrence_id": "2026-03-07T15:00:00+00:00",
            "tz": "Asia/Tokyo",
            "all_day": 1,
        }
        instance = adapter._instance_id({"external_id": "primary"}, event)
        self.assertEqual(instance, "google-master_20260308")

    def test_delete_occurrence_intent_receives_master_identity(self):
        store = Store(tempfile.mkdtemp(prefix="calendar-round2-google-instance-"))
        calendar_id = add_external_calendar(store, "series")
        adapter = RecordingAdapter()
        providers = OneAdapterProviders(adapter)
        master = store.insert_event(
            {
                "calendar_id": calendar_id,
                "uid": "series@example.test",
                "external_id": "provider-master",
                "href": "https://example.test/master.ics",
                "etag": '"m1"',
                "title": "Series",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "rrule": "FREQ=DAILY",
            }
        )
        result = ops.delete_event(
            store,
            providers,
            master["id"] + "@2026-10-02T09:00:00+00:00",
            scope="this",
        )
        self.assertEqual(result["assignments"][0]["status"], "done")
        update_event = next(call[2] for call in adapter.calls if call[0] == "update")
        self.assertEqual(update_event["master_external_id"], "provider-master")
        self.assertEqual(update_event["external_id"], "")

    def test_caldav_cancelled_occurrence_updates_master_resource(self):
        master_ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
            "UID:series-1\r\nSUMMARY:Series\r\nDTSTART;TZID=Europe/Moscow:20261001T120000\r\n"
            "DTEND;TZID=Europe/Moscow:20261001T130000\r\nRRULE:FREQ=DAILY\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        adapter = YandexAdapter("owner@yandex.test", "password")
        puts = []
        adapter.get = lambda href: (master_ics, '"m1"')
        adapter._request = lambda method, url, body=None, headers=None: (
            puts.append((method, url, body, headers)) or (204, {"etag": '"m2"'}, "")
        )
        event = {
            "master_id": "master",
            "master_href": "https://caldav.yandex.ru/calendars/owner/series.ics",
            "recurrence_id": "2026-10-02T09:00:00+00:00",
            "uid": "series-1",
            "title": "Series",
            "start_utc": "2026-10-02T09:00:00+00:00",
            "end_utc": "2026-10-02T10:00:00+00:00",
            "tz": "Europe/Moscow",
            "all_day": 0,
            "status": "cancelled",
            "reminders_json": "[]",
            "attendees_json": "[]",
        }
        adapter.update({}, event, "", {})
        self.assertEqual(puts[-1][1], event["master_href"])
        self.assertIn("RRULE:FREQ=DAILY", puts[-1][2])
        self.assertIn("EXDATE", puts[-1][2])

    def test_google_delete_honours_send_updates(self):
        adapter = self.google_adapter()
        event = {
            "external_id": "event-1",
            "attendees_json": '[{"email":"alice@example.test"}]',
        }
        adapter.delete(
            {"external_id": "primary"},
            event,
            '"e1"',
            {"send_updates": True},
        )
        method, _path, params, _body, headers = adapter.requests[-1]
        self.assertEqual((method, params, headers), ("DELETE", {"sendUpdates": "all"}, {"If-Match": '"e1"'}))

    def test_google_respond_updates_self_and_notifies_organizer_on_request(self):
        adapter = self.google_adapter()
        event = {
            "external_id": "invite-1",
            "attendees_json": (
                '[{"email":"owner@example.test","self":true,"status":"needsAction"},'
                '{"email":"organizer@example.test","status":"accepted"}]'
            ),
        }
        adapter.respond(
            {"external_id": "primary"},
            event,
            {"response": "accepted", "notify_organizer": True},
        )
        method, _path, params, body, _headers = adapter.requests[-1]
        self.assertEqual((method, params), ("PATCH", {"sendUpdates": "all"}))
        own = next(attendee for attendee in body["attendees"] if attendee["email"] == "owner@example.test")
        self.assertEqual(own["responseStatus"], "accepted")

    def test_yandex_respond_rewrites_own_partstat_and_increments_sequence(self):
        source = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
            "UID:invite-1\r\nSEQUENCE:7\r\nSUMMARY:Invite\r\n"
            "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n"
            "ORGANIZER:mailto:organizer@example.test\r\n"
            "ATTENDEE;CN=Owner;PARTSTAT=NEEDS-ACTION:mailto:owner@yandex.test\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        event = ics_to_rows(
            source,
            "yandex:owner@yandex.test:work",
            "https://caldav.yandex.ru/calendars/owner/invite.ics",
            '"e1"',
            get_tz("UTC"),
        )[0]
        adapter = YandexAdapter("owner@yandex.test", "password")
        puts = []
        adapter._request = lambda method, url, body=None, headers=None: (
            puts.append((method, url, body, headers)) or (204, {"etag": '"e2"'}, "")
        )
        adapter.respond({}, event, {"response": "accepted"})
        written = puts[-1][2]
        self.assertIn("PARTSTAT=ACCEPTED", written)
        self.assertIn("ORGANIZER:mailto:organizer@example.test", written)
        self.assertIn("SEQUENCE:8", written)


class ReminderRound2Tests(unittest.TestCase):
    def test_plan_keys_moved_exception_by_effective_start(self):
        ctx = make_context()
        now = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
        master = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Moved",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "rrule": "FREQ=DAILY",
                "reminders_json": "[15]",
            }
        )
        ctx.store.insert_event(
            {
                **{k: master.get(k) for k in ("calendar_id", "title", "tz", "all_day", "reminders_json")},
                "master_id": master["id"],
                "recurrence_id": "2026-10-01T09:00:00+00:00",
                "start_utc": "2026-10-01T11:00:00+00:00",
                "end_utc": "2026-10-01T12:00:00+00:00",
            }
        )
        rem.plan(ctx.store, lambda start, end: ctx.occurrences(start, end), now=now)
        starts = {row["occurrence_start_utc"] for row in ctx.store.upcoming_reminders(now, limit=20)}
        self.assertIn("2026-10-01T11:00:00+00:00", starts)
        self.assertNotIn("2026-10-01T09:00:00+00:00", starts)

    def test_shortened_exception_is_skipped_after_its_actual_end(self):
        ctx = make_context()
        start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        master = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Short exception",
                "start_utc": iso_utc(start),
                "end_utc": iso_utc(start + timedelta(hours=2)),
                "rrule": "FREQ=DAILY",
            }
        )
        ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Short exception",
                "master_id": master["id"],
                "recurrence_id": iso_utc(start),
                "start_utc": iso_utc(start),
                "end_utc": iso_utc(start + timedelta(minutes=10)),
            }
        )
        ctx.store.schedule_reminder(master["id"], iso_utc(start), 0, iso_utc(start), "cal:shortened")
        channel = Channel()
        stats = rem.deliver_due(ctx.store, channel, get_tz("UTC"), now=start + timedelta(minutes=30))
        self.assertEqual(channel.sent, [])
        self.assertEqual(stats["skipped"], 1)

    def test_exdated_occurrence_is_skipped_as_disappeared(self):
        ctx = make_context()
        start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        master = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Excluded",
                "start_utc": iso_utc(start),
                "end_utc": iso_utc(start + timedelta(hours=1)),
                "rrule": "FREQ=DAILY",
                "exdates": iso_utc(start),
            }
        )
        ctx.store.schedule_reminder(master["id"], iso_utc(start), 0, iso_utc(start), "cal:excluded")
        channel = Channel()
        stats = rem.deliver_due(ctx.store, channel, get_tz("UTC"), now=start)
        self.assertEqual(channel.sent, [])
        self.assertEqual(stats["skipped"], 1)

    def test_catchup_sends_one_batch_and_marks_all_sent(self):
        ctx = make_context()
        now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
        for index in range(2):
            event = ctx.store.insert_event(
                {
                    "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                    "title": f"Catchup {index}",
                    "start_utc": iso_utc(now - timedelta(minutes=5)),
                    "end_utc": iso_utc(now + timedelta(minutes=55)),
                    "reminders_json": "[15]",
                }
            )
            ctx.store.schedule_reminder(
                event["id"],
                event["start_utc"],
                15,
                iso_utc(now - timedelta(minutes=20)),
                f"cal:catchup:{index}",
            )
        channel = Channel()
        stats = rem.deliver_due(ctx.store, channel, get_tz("UTC"), now=now)
        self.assertEqual(len(channel.sent), 1)
        self.assertTrue(channel.sent[0][0].startswith("cal:batch:"))
        self.assertEqual((stats["sent"], stats["batched"]), (2, 2))

    def test_upcoming_includes_recently_sent(self):
        ctx = make_context()
        now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
        event = ctx.store.insert_event(
            {
                "calendar_id": DEFAULT_LOCAL_CALENDAR_ID,
                "title": "Recently sent",
                "start_utc": iso_utc(now + timedelta(minutes=5)),
                "end_utc": iso_utc(now + timedelta(hours=1)),
            }
        )
        ctx.store.schedule_reminder(event["id"], event["start_utc"], 15, iso_utc(now - timedelta(minutes=10)), "cal:recent")
        row = ctx.store.due_reminders(now)[0]
        ctx.store.mark_reminder(row["id"], "sent", "accepted")
        upcoming = rem.upcoming(ctx.store, get_tz("UTC"), now=now)
        self.assertEqual(upcoming[0]["state"], "sent")


class PendingDeleteAndContractRound2Tests(unittest.TestCase):
    def external_event(self, failure):
        store = Store(tempfile.mkdtemp(prefix="calendar-round2-delete-"))
        calendar_id = add_external_calendar(store, "delete")
        adapter = RecordingAdapter()
        adapter.fail_delete = failure
        providers = OneAdapterProviders(adapter)
        event = store.insert_event(
            {
                "calendar_id": calendar_id,
                "uid": "delete@example.test",
                "external_id": "remote-delete",
                "href": "https://example.test/delete.ics",
                "etag": '"e1"',
                "title": "Delete me",
                "start_utc": "2026-10-01T09:00:00+00:00",
                "end_utc": "2026-10-01T10:00:00+00:00",
                "origin": "external",
            }
        )
        return store, providers, adapter, event

    def test_pending_delete_stays_visible_then_companion_retry_removes_it(self):
        store, providers, _adapter, event = self.external_event("network")
        result = ops.delete_event(store, providers, event["id"], scope="all")
        self.assertEqual(result["assignments"][0]["status"], "pending")
        self.assertEqual(store.get_event(event["id"])["sync_state"], "pending_delete")
        visible = store.window(
            datetime(2026, 10, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        self.assertIn(event["id"], {row["id"] for row in visible})
        intent = store.open_intents()[0]
        store.settle_intent(intent["id"], "pending", {}, retry_in_sec=0)
        retried = ops.retry_due_intents(store, providers, owner="companion")
        self.assertEqual(retried[0]["status"], "done")
        self.assertIsNone(store.get_event(event["id"]))

    def test_delete_conflict_stays_visible_and_inspectable(self):
        store, providers, _adapter, event = self.external_event("conflict")
        result = ops.delete_event(store, providers, event["id"], scope="all")
        self.assertEqual(result["assignments"][0]["status"], "conflict")
        self.assertEqual(store.get_event(event["id"])["sync_state"], "conflict")
        self.assertIsNone(store.get_event(event["id"])["deleted_at"])

    def test_event_get_for_generated_occurrence_returns_that_occurrence_times(self):
        ctx = make_context()
        json.loads(
            tools.cal_create(
                ctx,
                title="Weekly",
                start="2026-10-01T09:00+00:00",
                rrule="FREQ=WEEKLY",
                confirm=True,
            )
        )
        occurrence = json.loads(
            tools.cal_events(ctx, start="2026-10-08", end="2026-10-09")
        )["events"][0]
        detail = json.loads(tools.cal_events(ctx, id=occurrence["id"]))["event"]
        self.assertEqual((detail["start"], detail["end"]), (occurrence["start"], occurrence["end"]))

    def test_large_update_preview_remains_needs_confirm_and_bounded(self):
        ctx = make_context()
        event = json.loads(
            tools.cal_create(
                ctx,
                title="Bounded update",
                start="2026-10-01T09:00+00:00",
                confirm=True,
            )
        )["event"]
        result_text = tools.cal_update(
            ctx,
            id=event["id"],
            attendees=[f"person-{index:05d}@example.test" for index in range(2000)],
            confirm=False,
        )
        self.assertLessEqual(len(result_text), 15_000)
        self.assertEqual(json.loads(result_text)["status"], "needs_confirm")

    def test_provider_error_records_wrong_google_token_key(self):
        from providers_google import save_tokens

        api = FakeAPI(
            {
                "GOOGLE_CALENDAR_CLIENT_ID": "client",
                "CALENDAR_TOKEN_KEY": "correct-key",
            }
        )
        save_tokens(
            api.state_dir,
            "correct-key",
            {
                "owner@example.test": {
                    "access_token": "a",
                    "refresh_token": "r",
                    "expires_at": "2026-10-01T00:00:00+00:00",
                }
            },
        )
        providers = RealProviders(
            {
                "GOOGLE_CALENDAR_CLIENT_ID": "client",
                "CALENDAR_TOKEN_KEY": "wrong-key",
            },
            api.state_dir,
        )
        account_id = "google:owner@example.test"
        self.assertIsNone(providers.adapter_for(account_id))
        self.assertIn("расшифровать", providers.errors[account_id])


class WidgetRouteRound2Tests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context()
        self.api = RouteAPI()
        routes.register_routes(self.api, lambda: self.ctx)

    @staticmethod
    def payload(response):
        return json.loads(response.body.decode("utf-8"))

    def test_widget_create_persists_attendees(self):
        response = asyncio.run(
            self.api.routes["event"](
                Request(
                    {
                        "title": "Widget invite",
                        "start": "2026-10-01T09:00+00:00",
                        "end": "2026-10-01T10:00+00:00",
                        "attendees": ["alice@example.test"],
                    }
                )
            )
        )
        event_id = self.payload(response)["event"]["id"]
        self.assertEqual(
            json.loads(self.ctx.store.get_event(event_id)["attendees_json"]),
            [{"email": "alice@example.test"}],
        )

    def test_widget_update_without_reminders_does_not_erase_them(self):
        event = json.loads(
            tools.cal_create(
                self.ctx,
                title="Keep reminders",
                start="2026-10-01T09:00+00:00",
                reminders=[30],
                confirm=True,
            )
        )["event"]
        response = asyncio.run(
            self.api.routes["event/update"](
                Request({"id": event["id"], "title": "Renamed"})
            )
        )
        self.assertEqual(self.payload(response)["status"], "ok")
        saved = self.ctx.store.get_event(event["id"])
        self.assertEqual((saved["title"], json.loads(saved["reminders_json"])), ("Renamed", [30]))


if __name__ == "__main__":
    unittest.main()
