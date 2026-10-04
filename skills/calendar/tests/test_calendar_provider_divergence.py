"""Provider fault/recovery regressions through the public cal_* tools and widget routes.

The real Yandex CalDAV and Google adapters run against in-memory servers that
can lose a response after applying a write or become unreachable. No personal
calendar, account, token or network is used; every store is a temporary SQLite
database and every date is relative to the current clock.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
for path in (SKILL, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_calendar_round2 import (  # noqa: E402
    Channel, OneAdapterProviders, Request, RouteAPI, add_external_calendar, make_context,
)
import ops  # noqa: E402
import reminders as rem  # noqa: E402
import routes  # noqa: E402
import tools  # noqa: E402
from model import get_tz, iso_utc, now_utc, parse_stored  # noqa: E402
from providers import YandexAdapter, _ical  # noqa: E402
from providers_google import GoogleAdapter  # noqa: E402
from scripts.worker import reconcile_calendar  # noqa: E402

UTC = timezone.utc


def _day(offset_days: int, hour: int = 9) -> datetime:
    base = now_utc().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(days=offset_days, hours=hour)


class FakeCalDAV:
    """caldav.yandex.ru in memory: href → (ics, etag); faults are injected per request."""

    def __init__(self):
        self.resources = {}
        self.calls = []
        self.down = False
        self.lose_response = set()   # methods whose next response is lost AFTER the server applied it
        self._version = 0

    def attach(self, adapter):
        adapter._request = self.request
        return adapter

    def _etag(self):
        self._version += 1
        return f'"v{self._version}"'

    def request(self, method, url, body=None, headers=None):
        headers = headers or {}
        self.calls.append((method, url, dict(headers)))
        if self.down:
            raise ops.ProviderError("network", "fake caldav unreachable")
        result = self._apply(method, url, body, headers)
        if method in self.lose_response:
            self.lose_response.discard(method)
            raise ops.ProviderError("network", "fake caldav response lost after apply")
        return result

    def _apply(self, method, url, body, headers):
        current = self.resources.get(url)
        if method in ("GET", "HEAD"):
            if current is None:
                raise ops.ProviderError("not_found", "404", 404)
            return 200, {"etag": current[1]}, current[0] if method == "GET" else ""
        if method == "PUT":
            if headers.get("If-None-Match") == "*" and current is not None:
                raise ops.ProviderError("conflict", "412 exists", 412)
            if headers.get("If-Match") and (current is None or current[1] != headers["If-Match"]):
                raise ops.ProviderError("conflict", "412 etag", 412)
            etag = self._etag()
            self.resources[url] = (body, etag)
            return 201, {"etag": etag}, ""
        if method == "DELETE":
            if current is None:
                raise ops.ProviderError("not_found", "404", 404)
            if headers.get("If-Match") and current[1] != headers["If-Match"]:
                raise ops.ProviderError("conflict", "412 etag", 412)
            del self.resources[url]
            return 204, {}, ""
        if method == "REPORT":
            parts = []
            for href, (text, etag) in sorted(self.resources.items()):
                if href.startswith(url):
                    parts.append(f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop><d:getetag>{escape(etag)}</d:getetag>"
                                 f"<c:calendar-data>{escape(text)}</c:calendar-data></d:prop></d:propstat></d:response>")
            xml = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                   + "".join(parts) + "</d:multistatus>")
            return 207, {}, xml
        raise ops.ProviderError("unsupported", method)

    def edit(self, href, mutate):
        """Another client rewrites the resource (new etag)."""
        icalendar = _ical()
        cal = icalendar.Calendar.from_ical(self.resources[href][0])
        mutate(cal)
        self.resources[href] = (cal.to_ical().decode("utf-8"), self._etag())


class FakeGoogle:
    """Calendar API v3 events in memory; DELETE leaves a cancelled tombstone like Google."""

    def __init__(self):
        self.events = {}
        self.calls = []
        self.down = False
        self.lose_response = set()
        self._version = 0

    def attach(self, adapter):
        adapter._request = self.request
        return adapter

    def _etag(self):
        self._version += 1
        return f'"g{self._version}"'

    def request(self, method, path, params=None, body=None, headers=None, _retry=True):
        self.calls.append((method, path, dict(params or {}), body))
        if self.down:
            raise ops.ProviderError("network", "fake google unreachable")
        result = self._apply(method, path, params or {}, body)
        if method in self.lose_response:
            self.lose_response.discard(method)
            raise ops.ProviderError("network", "fake google response lost after apply")
        return result

    def _apply(self, method, path, params, body):
        parts = path.split("/")
        if len(parts) == 4 and parts[3] == "events":
            if method == "POST":
                if body["id"] in self.events:
                    raise ops.ProviderError("http", "409 duplicate id", 409)
                item = {**body, "iCalUID": body["id"] + "@google.com", "etag": self._etag(), "status": body.get("status") or "confirmed"}
                self.events[body["id"]] = item
                return 200, {}, dict(item)
            show_deleted = params.get("showDeleted") == "true"
            items = [dict(i) for i in self.events.values() if show_deleted or i.get("status") != "cancelled"]
            return 200, {}, {"items": items, "nextSyncToken": f"sync-{self._version}"}
        event_id = urllib.parse.unquote(parts[4])
        item = self.events.get(event_id)
        if item is None:
            raise ops.ProviderError("not_found", "404", 404)
        if method == "GET":
            return 200, {}, dict(item)
        if method == "DELETE":
            if item.get("status") == "cancelled":
                raise ops.ProviderError("gone", "410 deleted", 410)
            item.update({"status": "cancelled", "etag": self._etag()})
            return 204, {}, {}
        if method in ("PATCH", "PUT"):
            item.update({k: v for k, v in (body or {}).items() if k != "id"})
            item["etag"] = self._etag()
            return 200, {}, dict(item)
        raise ops.ProviderError("unsupported", method)

    def live(self):
        return [i for i in self.events.values() if i.get("status") != "cancelled"]


def add_google_calendar(store):
    account = "google:owner@example.test"
    store.upsert_account({"id": account, "provider": "google", "alias": "g", "login": "owner@example.test", "status": "ok"})
    calendar_id = account + ":primary"
    store.upsert_calendar({"id": calendar_id, "account_id": account, "provider": "google", "external_id": "primary",
                           "href": "", "name": "Google", "writable": True, "publish_mode": "full"})
    return calendar_id


def caldav_context():
    ctx = make_context()
    ctx.tz = get_tz("UTC")
    calendar_id = add_external_calendar(ctx.store)
    server = FakeCalDAV()
    ctx.providers = OneAdapterProviders(server.attach(YandexAdapter("round2@example.test", "app-password")))
    return ctx, calendar_id, server


def google_context():
    ctx = make_context()
    ctx.tz = get_tz("UTC")
    calendar_id = add_google_calendar(ctx.store)
    server = FakeGoogle()
    adapter = GoogleAdapter("owner@example.test", "token", "refresh", now_utc() + timedelta(days=1), "client", "", None)
    ctx.providers = OneAdapterProviders(server.attach(adapter))
    return ctx, calendar_id, server


def run_open_intents(ctx, rounds=1):
    """The companion retry loop, without waiting for the backoff."""
    for _ in range(rounds):
        for intent in ctx.store.open_intents():
            if intent["state"] == "pending":
                ops.execute_intent(ctx.store, ctx.providers, intent, "companion")


def exhaust_create_retries(ctx, event_id):
    intent = next(i for i in ctx.store.intents_for_event(event_id) if i["kind"] == "create")
    while ctx.store.intents_for_event(event_id)[0]["state"] == "pending":
        ops.execute_intent(ctx.store, ctx.providers, intent, "companion")
    return ctx.store.intents_for_event(event_id)[0]


def sync(ctx, calendar_id, cursor=""):
    adapter = ctx.providers.adapter_for("")
    return reconcile_calendar(ctx.store, ctx.providers, adapter, ctx.store.get_calendar(calendar_id), ctx.tz, cursor)


def rows_for_calendar(ctx, calendar_id):
    with ctx.store._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events WHERE calendar_id=? AND master_id='' AND deleted_at IS NULL",
                                           (calendar_id,)).fetchall()]


class LostCreateResponseTests(unittest.TestCase):
    def test_caldav_owner_cancel_after_lost_create_reaches_the_server_after_recovery(self):
        ctx, calendar_id, server = caldav_context()
        start = _day(3)
        server.lose_response.add("PUT")
        created = json.loads(tools.cal_create(ctx, title="Lost answer", start=start.isoformat(), calendars=[calendar_id], confirm=True))
        self.assertEqual(created["status"], "pending")
        event_id = created["event"]["id"]
        self.assertEqual(len(server.resources), 1)                  # the server did create it
        server.down = True
        self.assertEqual(exhaust_create_retries(ctx, event_id)["state"], "failed")
        self.assertFalse(ctx.store.get_event(event_id)["href"])     # the local row never learnt its identity

        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        answer = asyncio.run(api.routes["event/delete"](Request({"id": event_id, "scope": "all"})))
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(json.loads(answer.body)["status"], "pending")   # unknown, not a false «deleted»
        self.assertEqual(ctx.store.get_event(event_id)["sync_state"], "pending_delete")

        server.down = False
        run_open_intents(ctx)
        self.assertEqual(server.resources, {})
        self.assertIsNone(ctx.store.get_event(event_id))
        self.assertFalse(json.loads(tools.cal_events(ctx, start=start.date().isoformat()))["events"])

    def test_caldav_cancel_when_failed_create_never_landed_closes_locally_without_remote_delete(self):
        ctx, calendar_id, server = caldav_context()
        server.down = True
        created = json.loads(tools.cal_create(ctx, title="Never landed", start=_day(3).isoformat(), calendars=[calendar_id], confirm=True))
        event_id = created["event"]["id"]
        exhaust_create_retries(ctx, event_id)
        server.down = False
        before = len(server.calls)
        out = json.loads(tools.cal_delete(ctx, id=event_id, scope="all", confirm=True))
        self.assertEqual(out["status"], "deleted")
        self.assertIsNone(ctx.store.get_event(event_id))
        self.assertEqual([call[0] for call in server.calls[before:]], ["GET"])   # one identity lookup, no write
        self.assertEqual(server.resources, {})

    def test_caldav_sync_adopts_identity_of_a_create_whose_answer_was_lost(self):
        ctx, calendar_id, server = caldav_context()
        server.lose_response.add("PUT")
        event_id = json.loads(tools.cal_create(ctx, title="Adopt me", start=_day(4).isoformat(), calendars=[calendar_id], confirm=True))["event"]["id"]
        server.down = True
        exhaust_create_retries(ctx, event_id)
        server.down = False
        sync(ctx, calendar_id)
        row = ctx.store.get_event(event_id)
        (href, (_text, etag)), = server.resources.items()
        self.assertEqual((row["href"], row["etag"]), (href, etag))
        self.assertEqual(len(rows_for_calendar(ctx, calendar_id)), 1)
        out = json.loads(tools.cal_update(ctx, id=event_id, title="Renamed", confirm=True))
        self.assertEqual(out["status"], "updated")
        puts = [call for call in server.calls if call[0] == "PUT"]
        self.assertEqual(puts[-1][2].get("If-Match"), etag)        # an update of the same resource, not a second create
        self.assertEqual(len(server.resources), 1)

    def test_google_lost_create_is_adopted_not_duplicated_and_cancel_reaches_google(self):
        ctx, calendar_id, server = google_context()
        start = _day(3)
        server.lose_response.add("POST")
        created = json.loads(tools.cal_create(ctx, title="Google lost", start=start.isoformat(), calendars=[calendar_id], confirm=True))
        event_id = created["event"]["id"]
        self.assertEqual(created["status"], "pending")
        sync(ctx, calendar_id)                                     # the companion reads the feed before the create retry
        rows = rows_for_calendar(ctx, calendar_id)
        self.assertEqual([r["id"] for r in rows], [event_id])
        (remote,) = server.live()
        self.assertEqual(rows[0]["external_id"], remote["id"])
        run_open_intents(ctx)
        self.assertEqual(len(server.live()), 1)
        out = json.loads(tools.cal_delete(ctx, id=event_id, scope="all", confirm=True))
        self.assertEqual(out["status"], "deleted")
        self.assertEqual(server.live(), [])
        sync(ctx, calendar_id, cursor="sync-token")
        self.assertEqual(rows_for_calendar(ctx, calendar_id), [])
        self.assertFalse(json.loads(tools.cal_events(ctx, start=start.date().isoformat()))["events"])

    def test_google_cancel_after_lost_create_and_exhausted_retries_deletes_remote(self):
        ctx, calendar_id, server = google_context()
        server.lose_response.add("POST")
        event_id = json.loads(tools.cal_create(ctx, title="Google exhausted", start=_day(5).isoformat(), calendars=[calendar_id], confirm=True))["event"]["id"]
        server.down = True
        exhaust_create_retries(ctx, event_id)
        out = json.loads(tools.cal_delete(ctx, id=event_id, scope="all", confirm=True))
        self.assertEqual(out["status"], "pending")
        server.down = False
        run_open_intents(ctx)
        self.assertEqual(server.live(), [])
        self.assertIsNone(ctx.store.get_event(event_id))

    def test_edit_made_while_the_create_outcome_is_unknown_reaches_the_provider(self):
        for name, factory, write in (("caldav", caldav_context, "PUT"), ("google", google_context, "POST")):
            for exhausted in (False, True):
                with self.subTest(provider=name, create_retries_exhausted=exhausted):
                    ctx, calendar_id, server = factory()
                    server.lose_response.add(write)
                    event_id = json.loads(tools.cal_create(ctx, title="Before", start=_day(3).isoformat(),
                                                           calendars=[calendar_id], confirm=True))["event"]["id"]
                    if exhausted:
                        server.down = True
                        exhaust_create_retries(ctx, event_id)
                        server.down = False
                    json.loads(tools.cal_update(ctx, id=event_id, title="After", confirm=True))
                    run_open_intents(ctx, rounds=2)
                    if name == "caldav":
                        (text, etag), = server.resources.values()
                        self.assertIn("SUMMARY:After", text)
                    else:
                        (remote,) = server.live()
                        self.assertEqual(remote["summary"], "After")
                        etag = remote["etag"]
                    row = ctx.store.get_event(event_id)
                    self.assertEqual((row["title"], row["etag"], row["sync_state"]), ("After", etag, "synced"))
                    self.assertFalse(ctx.store.open_intents())

    def test_identity_learnt_by_sync_requeues_a_delete_parked_as_identity_conflict(self):
        ctx, calendar_id, server = caldav_context()
        server.lose_response.add("PUT")
        event_id = json.loads(tools.cal_create(ctx, title="Legacy conflict", start=_day(3).isoformat(), calendars=[calendar_id], confirm=True))["event"]["id"]
        server.down = True
        exhaust_create_retries(ctx, event_id)
        # An adapter without identity lookup (or an older build) leaves the delete in conflict.
        with patch.object(YandexAdapter, "lookup_created", None, create=True):
            out = json.loads(tools.cal_delete(ctx, id=event_id, scope="all", confirm=True))
        self.assertEqual(out["status"], "conflict")
        server.down = False
        sync(ctx, calendar_id)
        run_open_intents(ctx)
        self.assertEqual(server.resources, {})
        self.assertIsNone(ctx.store.get_event(event_id))


class MovedCaldavOccurrenceCancellationTests(unittest.TestCase):
    def _moved_series(self):
        ctx, calendar_id, server = caldav_context()
        first = _day(2)
        created = json.loads(tools.cal_create(ctx, title="Standup", start=first.isoformat(), duration_min=30,
                                              rrule="FREQ=DAILY;COUNT=5", reminders=[15], calendars=[calendar_id], confirm=True))
        master_id = created["event"]["id"]
        slot = first + timedelta(days=1)
        moved_to = slot + timedelta(hours=2)
        out = json.loads(tools.cal_update(ctx, id=f"{master_id}@{iso_utc(slot)}", start=moved_to.isoformat(), scope="this", confirm=True))
        self.assertEqual(out["status"], "updated")
        href = ctx.store.get_event(master_id)["href"]
        self.assertIn("RECURRENCE-ID", server.resources[href][0])
        return ctx, calendar_id, server, master_id, slot, moved_to, href

    def _visible_on(self, ctx, day):
        return json.loads(tools.cal_events(ctx, start=day.date().isoformat()))["events"]

    def _server_cancels(self, server, href, slot, keep_override=False):
        def mutate(cal):
            master = next(c for c in cal.walk("VEVENT") if c.get("RECURRENCE-ID") is None)
            master.add("EXDATE", slot.astimezone(get_tz("UTC")))
            if not keep_override:
                for comp in [c for c in cal.walk("VEVENT") if c.get("RECURRENCE-ID") is not None]:
                    cal.subcomponents.remove(comp)
        server.edit(href, mutate)

    def test_server_exdate_cancel_of_a_moved_occurrence_removes_the_local_exception(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        moved = self._visible_on(ctx, slot)
        self.assertEqual([e["start"][:16] for e in moved], [moved_to.isoformat()[:16]])
        self._server_cancels(server, href, slot)
        sync(ctx, calendar_id)
        self.assertEqual(self._visible_on(ctx, slot), [])
        self.assertFalse([e for e in ctx.store.exceptions_for(master_id) if e["recurrence_id"] == iso_utc(slot)])
        api = RouteAPI()
        routes.register_routes(api, lambda: ctx)
        agenda = asyncio.run(api.routes["agenda"](Request(query={"date": slot.date().isoformat()})))
        self.assertEqual(json.loads(agenda.body)["events"], [])
        card = asyncio.run(api.routes["event/get"](Request(query={"id": moved[0]["id"]})))
        self.assertEqual(card.status_code, 404)                     # the stale exception is not reachable from a card
        # the other dates of the series are untouched
        self.assertEqual(len(self._visible_on(ctx, slot + timedelta(days=1))), 1)

    def test_master_exdate_wins_over_an_override_the_server_kept(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        self._server_cancels(server, href, slot, keep_override=True)
        sync(ctx, calendar_id)
        self.assertEqual(self._visible_on(ctx, slot), [])

    def test_reminder_for_a_moved_occurrence_is_skipped_after_server_exdate_cancel(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        rem.set_mode(ctx.store, calendar_id, True)                  # «напоминает Уроборос» for this external calendar
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e, strict=True), now=moved_to - timedelta(minutes=30))
        with ctx.store._conn() as c:
            planned = [dict(r) for r in c.execute("SELECT * FROM reminders WHERE recurrence_id=?", (iso_utc(slot),)).fetchall()]
        self.assertEqual([r["occurrence_start_utc"] for r in planned], [iso_utc(moved_to)])
        self._server_cancels(server, href, slot, keep_override=True)   # even while the stale override row exists
        sync(ctx, calendar_id)
        channel = Channel()
        fire = parse_stored(planned[0]["fire_at_utc"])
        with patch.object(rem, "now_utc", return_value=fire):
            stats = rem.deliver_due(ctx.store, channel, ctx.tz, now=fire)
        self.assertEqual(channel.sent, [])
        self.assertEqual(stats["sent"], 0)

    def test_owner_cancel_of_a_moved_occurrence_stays_cancelled_through_sync(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        moved = self._visible_on(ctx, slot)[0]
        out = json.loads(tools.cal_delete(ctx, id=moved["id"], scope="this", confirm=True))
        self.assertEqual(out["status"], "deleted")
        text = server.resources[href][0]
        self.assertNotIn("RECURRENCE-ID", text)
        self.assertIn("EXDATE", text)
        self.assertEqual(self._visible_on(ctx, slot), [])
        server.edit(href, lambda cal: None)                         # any later remote touch re-reads the resource
        sync(ctx, calendar_id)
        self.assertEqual(self._visible_on(ctx, slot), [])
        self.assertEqual(len(self._visible_on(ctx, slot + timedelta(days=1))), 1)

    def test_server_revert_of_a_move_restores_the_original_slot_locally(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()

        def drop_override(cal):
            for comp in [c for c in cal.walk("VEVENT") if c.get("RECURRENCE-ID") is not None]:
                cal.subcomponents.remove(comp)
        server.edit(href, drop_override)
        sync(ctx, calendar_id)
        self.assertEqual([e["start"][:16] for e in self._visible_on(ctx, slot)], [slot.isoformat()[:16]])

    def test_pending_local_move_is_not_erased_by_a_sync_that_has_not_seen_it(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        later = slot + timedelta(days=1)
        server.down = True
        out = json.loads(tools.cal_update(ctx, id=f"{master_id}@{iso_utc(later)}", start=(later + timedelta(hours=1)).isoformat(),
                                          scope="this", confirm=True))
        self.assertEqual(out["status"], "pending")
        server.down = False
        server.edit(href, lambda cal: None)
        sync(ctx, calendar_id)
        self.assertEqual([e["start"][:16] for e in self._visible_on(ctx, later)], [(later + timedelta(hours=1)).isoformat()[:16]])
        run_open_intents(ctx)
        self.assertEqual(server.resources[href][0].count("RECURRENCE-ID"), 2)

    def test_following_delete_retires_stale_exception_and_keeps_pending_move(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        cut = slot + timedelta(days=1)
        plan_now = slot - timedelta(minutes=15)
        rem.set_mode(ctx.store, calendar_id, True)
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e, strict=True), now=plan_now)
        with ctx.store._conn() as c:
            queued = c.execute(
                "SELECT 1 FROM reminders WHERE event_id=? AND recurrence_id=? AND state='scheduled'",
                (master_id, iso_utc(cut))).fetchone()
        self.assertIsNotNone(queued)
        server.down = True
        kept_to = moved_to + timedelta(hours=1)
        kept = json.loads(tools.cal_update(
            ctx, id=f"{master_id}@{iso_utc(slot)}", start=kept_to.isoformat(),
            title="Kept edit", scope="this", confirm=True))
        stale = json.loads(tools.cal_update(
            ctx, id=f"{master_id}@{iso_utc(cut)}",
            start=(cut + timedelta(hours=1)).isoformat(), scope="this", confirm=True))
        self.assertEqual(kept["status"], "pending")
        self.assertEqual(stale["status"], "pending")
        exceptions = ctx.store.exceptions_for(master_id)
        kept_id = next(e["id"] for e in exceptions if e["recurrence_id"] == iso_utc(slot))
        stale_id = next(e["id"] for e in exceptions if e["recurrence_id"] == iso_utc(cut))
        server.down = False
        deleted = json.loads(tools.cal_delete(
            ctx, id=f"{master_id}@{iso_utc(cut)}", scope="following", confirm=True))
        self.assertEqual(deleted["status"], "deleted")
        self.assertFalse(ctx.store.get_event(kept_id).get("deleted_at"))
        self.assertTrue(ctx.store.get_event(stale_id).get("deleted_at"))
        before_retry = len(server.calls)
        run_open_intents(ctx, rounds=4)
        self.assertEqual([call[0] for call in server.calls[before_retry:]], ["GET", "PUT"])
        sync(ctx, calendar_id)
        self.assertEqual(self._visible_on(ctx, cut), [])
        self.assertEqual([e["start"][:16] for e in self._visible_on(ctx, slot)], [kept_to.isoformat()[:16]])
        self.assertIn("Kept edit", [e["title"] for e in self._visible_on(ctx, moved_to)])
        self.assertIn("SUMMARY:Kept edit", server.resources[href][0])
        self.assertEqual(server.resources[href][0].count("RECURRENCE-ID"), 1)
        self.assertEqual(ctx.store.intents_for_event(stale_id)[-1]["state"], ops.INTENT_DONE)
        self.assertEqual(ctx.store.intents_for_event(kept_id)[-1]["state"], ops.INTENT_DONE)
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e, strict=True), now=plan_now)
        with ctx.store._conn() as c:
            queued = c.execute(
                "SELECT recurrence_id FROM reminders WHERE event_id=? AND state IN ('scheduled', 'no_channel')",
                (master_id,)).fetchall()
        recurrence_ids = [r["recurrence_id"] for r in queued]
        self.assertIn(iso_utc(slot), recurrence_ids)
        self.assertFalse([rid for rid in recurrence_ids if rid >= iso_utc(cut)])

    def test_all_delete_retires_parked_exception_before_provider_lookup(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        server.down = True
        changed = json.loads(tools.cal_update(ctx, id=f"{master_id}@{iso_utc(slot)}",
                                             start=(moved_to + timedelta(hours=1)).isoformat(),
                                             scope="this", confirm=True))
        self.assertEqual(changed["status"], "pending")
        exception = ctx.store.exceptions_for(master_id)[0]
        parked = ctx.store.intents_for_event(exception["id"])[-1]
        server.down = False
        deleted = json.loads(tools.cal_delete(ctx, id=master_id, scope="all", confirm=True))
        self.assertEqual(deleted["status"], "deleted")
        self.assertIsNone(ctx.store.get_event(master_id))
        before = len(server.calls)
        result = ops.execute_intent(ctx.store, None, parked, "companion")
        self.assertEqual(result["status"], ops.INTENT_DONE)
        settled = ctx.store.intents_for_event(exception["id"])[-1]
        self.assertEqual(json.loads(settled["result_json"])["note"], "superseded by deletion")
        self.assertEqual(len(server.calls), before)
        self.assertFalse(server.resources)
        self.assertEqual(self._visible_on(ctx, slot), [])

    def test_leased_exception_rsvp_rechecks_master_deletion_without_provider_call(self):
        ctx, calendar_id, server, master_id, slot, moved_to, href = self._moved_series()
        exception = ctx.store.exceptions_for(master_id)[0]
        calendar = ctx.store.get_calendar(calendar_id)
        intent = ctx.store.add_intent("rsvp", calendar["account_id"], calendar_id,
                                      exception["id"], {"response": "declined"})
        leased = ctx.store.lease_intent(intent["id"], "companion")
        self.assertIsNotNone(leased)
        # The companion already leased the operation when deletion withdrew it.
        ctx.store.delete_event(master_id)
        before = len(server.calls)
        with patch.object(ctx.providers, "adapter_for", side_effect=AssertionError("no provider lookup")):
            result = ops.run_leased_intent(ctx.store, ctx.providers, leased)
        self.assertEqual(result["status"], ops.INTENT_DONE)
        self.assertEqual(len(server.calls), before)
        self.assertEqual(json.loads(ctx.store.intents_for_event(exception["id"])[-1]["result_json"])["note"],
                         "superseded by deletion")


if __name__ == "__main__":
    unittest.main()
