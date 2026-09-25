"""Offline tests for the calendar skill (no network, no host).

Run from the skill directory with an interpreter that has icalendar + cryptography:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

import ops  # noqa: E402
import reminders as rem  # noqa: E402
import tools  # noqa: E402
from model import DEFAULT_LOCAL_CALENDAR_ID, get_tz, iso_utc, parse_input, parse_stored  # noqa: E402
from store import Store  # noqa: E402


class FakeAPI:
    def __init__(self, secrets=None):
        self.dir = tempfile.mkdtemp()
        self.secrets = secrets or {}

    def get_state_dir(self):
        return self.dir

    def get_settings(self, keys):
        return {k: self.secrets.get(k, "") for k in keys}


class FakeAdapter:
    """Records calls; ``fail`` makes the next call raise the given ProviderError kind."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.counter = 0

    def _maybe_fail(self):
        if self.fail:
            kind, self.fail = self.fail, None
            raise ops.ProviderError(kind, f"simulated {kind}")

    def create(self, cal, event, payload):
        self.calls.append(("create", event["title"]))
        self._maybe_fail()
        self.counter += 1
        return {"external_id": f"ext{self.counter}", "href": f"https://x/{self.counter}.ics", "etag": f"e{self.counter}"}

    def update(self, cal, event, expected_etag, payload):
        self.calls.append(("update", event["title"], expected_etag))
        self._maybe_fail()
        return {"etag": expected_etag + "+"}

    def delete(self, cal, event, expected_etag, payload=None):
        self.calls.append(("delete", event["title"]))
        self._maybe_fail()

    def respond(self, cal, event, payload):
        self.calls.append(("respond", event["title"], payload.get("response")))
        self._maybe_fail()
        return {"etag": (event.get("etag") or "") + "+r"}


class FakeProviders:
    def __init__(self, adapter):
        self.adapter = adapter

    def adapter_for(self, account_id):
        return self.adapter if account_id.startswith("yandex:") else None


def make_ctx(secrets=None):
    return tools.Context(FakeAPI(secrets))


def add_external_calendar(store, name="Рабочий", publish_mode="busy"):
    store.upsert_account({"id": "yandex:u@ya.ru", "provider": "yandex", "alias": "работа", "login": "u@ya.ru", "status": "ok"})
    store.upsert_calendar({"id": "yandex:u@ya.ru:events-1", "account_id": "yandex:u@ya.ru", "provider": "yandex", "external_id": "events-1",
                           "href": "https://caldav.yandex.ru/calendars/u%40ya.ru/events-1/", "name": name, "writable": True,
                           "role_publish": True, "publish_mode": publish_mode})
    return "yandex:u@ya.ru:events-1"


class StoreTests(unittest.TestCase):
    def test_prototype_migration_keeps_local_events_and_backs_up(self):
        import sqlite3
        d = tempfile.mkdtemp()
        path = os.path.join(d, "calendar.sqlite3")
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE events (id TEXT PRIMARY KEY, calendar TEXT NOT NULL, title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,"
                      " all_day INTEGER NOT NULL DEFAULT 0, location TEXT, description TEXT, remind_min INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,"
                      " updated TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'local', calendar_name TEXT NOT NULL DEFAULT 'Личное', uid TEXT, href TEXT, etag TEXT, recurring INTEGER NOT NULL DEFAULT 0)")
            c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            c.execute("CREATE TABLE calendars (id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, href TEXT, writable INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL)")
            c.execute("INSERT INTO events (id, calendar, title, start, end, remind_min, created, updated, source) VALUES ('old1','local:personal','Тренировка','2026-09-22T19:00:00+04:00','2026-09-22T20:00:00+04:00',30,'x','x','local')")
            c.execute("INSERT INTO events (id, calendar, title, start, end, remind_min, created, updated, source) VALUES ('old2','yandex:a:b','Чужое','2026-09-22T19:00:00+04:00','2026-09-22T20:00:00+04:00',0,'x','x','yandex')")
        store = Store(d)
        ev = store.get_event("old1")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["calendar_id"], DEFAULT_LOCAL_CALENDAR_ID)
        self.assertEqual(json.loads(ev["reminders_json"]), [30])
        self.assertIsNone(store.get_event("old2"))
        self.assertTrue(os.listdir(os.path.join(d, "backup")))
        self.assertTrue(store.get_setting("migrated_from_prototype"))

    def test_intent_lease_is_exclusive_and_settles(self):
        store = Store(tempfile.mkdtemp())
        row = store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": "x", "start_utc": "2026-09-25T06:00:00+00:00", "end_utc": "2026-09-25T07:00:00+00:00"})
        intent = store.add_intent("create", "yandex:u", "yandex:u:c", row["id"], {})
        self.assertIsNotNone(store.lease_intent(intent["id"], "child:1"))
        self.assertIsNone(store.lease_intent(intent["id"], "companion"))
        store.settle_intent(intent["id"], "done", {})
        self.assertEqual(store.intent_counts(), {"done": 1})


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tz = get_tz("Asia/Dubai")
        self.ctx.tz = self.tz

    def create(self, **kw):
        kw.setdefault("confirm", True)
        return json.loads(tools.cal_create(self.ctx, **kw))

    def test_create_and_free_windows(self):
        r = self.create(title="Обед", start="2026-09-28T13:00", duration_min=60)
        self.assertEqual(r["status"], "created")
        f = json.loads(tools.cal_free(self.ctx, start="2026-09-28T09:00", end="2026-09-28T19:00", duration_min=60))
        starts = [s["start"][11:16] for s in f["slots"]]
        self.assertEqual(starts, ["09:00", "14:00"])

    def test_recurring_scopes_this_following_all(self):
        self.create(title="Обычно завтрак", start="2026-09-28T06:30", duration_min=30, hidden=True, availability="soft", rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
        week = json.loads(tools.cal_events(self.ctx, start="2026-09-28", end="2026-10-03"))["events"]
        self.assertEqual(len(week), 5)
        self.assertTrue(all(e["visibility"] == "hidden" and e["availability"] == "soft" for e in week))
        # this: one date moves, the rest stay
        u = json.loads(tools.cal_update(self.ctx, id=week[1]["id"], start="2026-09-29T07:00", scope="this", confirm=True))
        self.assertEqual(u["status"], "updated")
        day = json.loads(tools.cal_events(self.ctx, start="2026-09-29", end="2026-09-30"))["events"]
        self.assertEqual([e["start"][11:16] for e in day], ["07:00"])
        # following: split series
        f = json.loads(tools.cal_update(self.ctx, id=week[3]["id"], start="2026-10-01T07:30", scope="following", confirm=True))
        self.assertEqual(f["status"], "updated")
        after = json.loads(tools.cal_events(self.ctx, start="2026-09-30", end="2026-10-08"))["events"]
        self.assertEqual([e["start"][11:16] for e in after], ["06:30", "07:30", "07:30", "07:30", "07:30", "07:30"])
        # all: delete the new series entirely
        d = json.loads(tools.cal_delete(self.ctx, id=after[1]["series_id"], scope="all", confirm=True))
        self.assertEqual(d["status"], "deleted")
        left = json.loads(tools.cal_events(self.ctx, start="2026-09-30", end="2026-10-08"))["events"]
        self.assertEqual([e["start"][11:16] for e in left], ["06:30"])

    def test_ambiguous_scope_asks(self):
        self.create(title="Серия", start="2026-09-28T09:00", rrule="FREQ=DAILY")
        ev = json.loads(tools.cal_events(self.ctx, start="2026-09-28", end="2026-09-29"))["events"][0]
        r = json.loads(tools.cal_delete(self.ctx, id=ev["series_id"], confirm=True))
        self.assertEqual(r["status"], "ambiguous")

    def test_linked_copies_busy_mode_and_partial_failure(self):
        cal_id = add_external_calendar(self.ctx.store)
        adapter = FakeAdapter()
        self.ctx.providers = FakeProviders(adapter)
        r = self.create(title="Приём у врача", start="2026-09-28T15:00", duration_min=30, calendars="local:personal, Рабочий", location="клиника")
        self.assertEqual(r["status"], "created")
        members = self.ctx.store.group_members(r["event"]["link_group_id"])
        titles = sorted(m["title"] for m in members)
        self.assertEqual(titles, ["Занят", "Приём у врача"])
        copy = [m for m in members if m["calendar_id"] == cal_id][0]
        self.assertEqual(copy["location"], "")
        self.assertEqual(copy["external_id"], "ext1")
        # network failure on update: intent stays pending, event marked pending
        adapter.fail = "network"
        u = json.loads(tools.cal_update(self.ctx, id=r["event"]["id"], start="2026-09-28T16:00", confirm=True))
        self.assertEqual(u["status"], "pending")
        statuses = {a["calendar_id"]: a["status"] for a in u["assignments"]}
        self.assertEqual(statuses["local:personal"], "done")
        self.assertEqual(statuses[cal_id], "pending")
        self.assertEqual(self.ctx.store.intent_counts().get("pending"), 1)
        # companion retry succeeds and clears the backlog
        self.ctx.store.set_setting("_", 1)
        for intent in self.ctx.store.open_intents():
            self.ctx.store.settle_intent(intent["id"], "pending", {}, retry_in_sec=0)
        retried = ops.retry_due_intents(self.ctx.store, self.ctx.providers, owner="companion")
        self.assertEqual([r_["status"] for r_ in retried], ["done"])
        self.assertEqual(self.ctx.store.get_event(copy["id"])["start_utc"], iso_utc(parse_input("2026-09-28T16:00", self.tz)[0]))

    def test_delete_cascades_to_copies_and_conflict_is_reported(self):
        cal_id = add_external_calendar(self.ctx.store)
        adapter = FakeAdapter()
        self.ctx.providers = FakeProviders(adapter)
        r = self.create(title="Обед", start="2026-09-28T13:00", calendars="local:personal, Рабочий")
        adapter.fail = "conflict"
        d = json.loads(tools.cal_delete(self.ctx, id=r["event"]["id"], confirm=True))
        self.assertIn(d["status"], ("conflict", "deleted_partially", "pending"))   # a provider conflict is reported as such
        self.assertEqual(self.ctx.store.intent_counts().get("conflict"), 1)


class ReminderTests(unittest.TestCase):
    def test_plan_and_deliver_without_channel(self):
        ctx = make_ctx()
        from model import now_utc
        start = now_utc() + timedelta(minutes=10)
        json.loads(tools.cal_create(ctx, title="Скоро", start=start.isoformat(), duration_min=30, reminders=[15], confirm=True))
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e))  # idempotent: cal_create already planned it
        self.assertEqual(len(ctx.store.due_reminders(now_utc())), 1)

        class NoRoute(rem.NotifyChannel):
            def state(self, refresh=False):
                return "no_route"
        stats = rem.deliver_due(ctx.store, NoRoute(), ctx.tz)
        self.assertEqual(stats["no_channel"], 1)
        up = rem.upcoming(ctx.store, ctx.tz)
        self.assertEqual(up[0]["state"], "no_channel")

    def test_batch_notice_id_is_short(self):
        ids = [rem.notice_id_for("evt_%d" % i, "2026-09-25T06:00:00+00:00", 15) for i in range(50)]
        bid = rem.batch_notice_id(ids)
        self.assertTrue(bid.startswith("cal:batch:") and len(bid) <= 128)


class ProviderRoundTripTests(unittest.TestCase):
    def test_ics_round_trip_preserves_unknown_props_and_exceptions(self):
        from providers import ics_to_rows, row_to_ics
        ics = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nBEGIN:VEVENT\nUID:abc\nSUMMARY:Тренировка\n"
               "DTSTART;TZID=Europe/Moscow:20260928T190000\nDTEND;TZID=Europe/Moscow:20260928T200000\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE\n"
               "X-YANDEX-FOO:bar\nBEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT30M\nEND:VALARM\nEND:VEVENT\n"
               "BEGIN:VEVENT\nUID:abc\nRECURRENCE-ID;TZID=Europe/Moscow:20260930T190000\nSUMMARY:Перенос\n"
               "DTSTART;TZID=Europe/Moscow:20260930T200000\nDTEND;TZID=Europe/Moscow:20260930T210000\nEND:VEVENT\nEND:VCALENDAR\n")
        rows = ics_to_rows(ics, "c", "h", "e1", get_tz("UTC"))
        self.assertEqual(len(rows), 2)
        master, exc = rows
        self.assertEqual(master["rrule"], "FREQ=WEEKLY;BYDAY=MO,WE")
        self.assertEqual(json.loads(master["reminders_json"]), [30])
        self.assertEqual(exc["recurrence_id"], "2026-09-30T16:00:00+00:00")
        out = row_to_ics({**master, "title": "Тренировка 2"})
        self.assertIn("X-YANDEX-FOO:bar", out)
        self.assertIn("SUMMARY:Тренировка 2", out)
        self.assertIn("BEGIN:VTIMEZONE", out)


if __name__ == "__main__":
    unittest.main()


class GoogleConversionTests(unittest.TestCase):
    def test_gevent_round_trip_and_stable_id(self):
        from providers_google import gevent_to_row, row_to_gevent, _google_id
        item = {"id": "abc123", "iCalUID": "abc123@google.com", "etag": '"e1"', "summary": "Standup",
                "start": {"dateTime": "2026-09-28T10:00:00+03:00", "timeZone": "Europe/Moscow"},
                "end": {"dateTime": "2026-09-28T10:30:00+03:00", "timeZone": "Europe/Moscow"},
                "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE", "EXDATE;TZID=Europe/Moscow:20261005T100000"],
                "attendees": [{"email": "me@x.com", "self": True, "responseStatus": "accepted"}],
                "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]}, "status": "confirmed"}
        row = gevent_to_row(item, "google:me@x.com:primary", get_tz("UTC"))
        self.assertEqual(row["start_utc"], "2026-09-28T07:00:00+00:00")
        self.assertEqual(row["exdates"], "2026-10-05T07:00:00+00:00")
        self.assertEqual(row["my_response"], "accepted")
        body = row_to_gevent({**row, "title": "Standup 2"})
        self.assertEqual(body["start"], {"dateTime": "2026-09-28T10:00:00+03:00", "timeZone": "Europe/Moscow"})
        self.assertIn("EXDATE:20261005T070000Z", body["recurrence"])
        self.assertEqual(body["reminders"], {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]})
        gid = _google_id({"uid": "u-1"})
        self.assertEqual(gid, _google_id({"uid": "u-1"}))
        self.assertTrue(all(ch in "abcdefghijklmnopqrstuv0123456789" for ch in gid) and 5 <= len(gid) <= 1024)
        cancelled = gevent_to_row({"id": "abc123_20260930T070000Z", "recurringEventId": "abc123", "status": "cancelled",
                                   "originalStartTime": {"dateTime": "2026-09-30T10:00:00+03:00"},
                                   "start": {"dateTime": "2026-09-30T10:00:00+03:00"}, "end": {"dateTime": "2026-09-30T10:30:00+03:00"}}, "c", get_tz("UTC"))
        self.assertEqual((cancelled["recurrence_id"], cancelled["status"], cancelled["master_external_id"]), ("2026-09-30T07:00:00+00:00", "cancelled", "abc123"))

    def test_token_vault_roundtrip_and_wrong_key(self):
        from providers_google import load_tokens, save_tokens
        d = tempfile.mkdtemp()
        save_tokens(d, "фраза-владельца", {"me@x.com": {"refresh_token": "r", "access_token": "a", "expires_at": "2026-09-25T00:00:00+00:00"}})
        self.assertEqual(load_tokens(d, "фраза-владельца")["me@x.com"]["refresh_token"], "r")
        with self.assertRaises(ops.ProviderError):
            load_tokens(d, "другая")


class DecisionSemanticsTests(unittest.TestCase):
    """Owner decisions that the audit found violated: 5 A ('all'), 19 A/20 A (reminders opt-in per external calendar)."""

    def test_all_means_default_plus_publish_set_not_every_writable(self):
        ctx = make_ctx()
        busy_cal = add_external_calendar(ctx.store, name="Рабочий")           # role_publish=True
        ctx.store.upsert_calendar({"id": "yandex:u@ya.ru:events-2", "account_id": "yandex:u@ya.ru", "provider": "yandex", "external_id": "events-2",
                                   "href": "https://caldav.yandex.ru/x/", "name": "Хобби", "writable": True, "role_publish": False})
        ids, err = ctx.resolve_calendars("all")
        self.assertEqual(err, "")
        self.assertEqual(ids, [DEFAULT_LOCAL_CALENDAR_ID, busy_cal])

    def test_external_calendar_reminders_only_after_enable(self):
        ctx = make_ctx()
        cal_id = add_external_calendar(ctx.store)
        from model import now_utc
        start = now_utc() + timedelta(minutes=20)
        row = ctx.store.insert_event({"calendar_id": cal_id, "title": "Внешнее", "start_utc": iso_utc(start), "end_utc": iso_utc(start + timedelta(hours=1)),
                                      "reminders_json": "[15]", "origin": "external", "external_id": "x1", "href": "https://caldav.yandex.ru/x/x1.ics"})
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e))
        self.assertEqual(ctx.store.upcoming_reminders(now_utc()), [])          # provider reminds; Ouroboros silent (20 A)
        rem.set_mode(ctx.store, cal_id, True)
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e))
        self.assertEqual(len(ctx.store.upcoming_reminders(now_utc())), 1)      # mode on: Ouroboros takes over
        local = ctx.store.insert_event({"calendar_id": DEFAULT_LOCAL_CALENDAR_ID, "title": "Локальное без правил", "start_utc": iso_utc(start), "end_utc": iso_utc(start + timedelta(hours=1))})
        rem.plan(ctx.store, lambda s, e: ctx.occurrences(s, e))
        self.assertEqual(len([r for r in ctx.store.upcoming_reminders(now_utc()) if r["event_id"] == local["id"]]), 0)   # 19 A: no default rule → nothing


class YandexExceptionWriteTests(unittest.TestCase):
    def test_exception_is_written_inside_master_resource(self):
        from providers import YandexAdapter
        master_ics = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nBEGIN:VEVENT\nUID:ser1\nSUMMARY:Серия\n"
                      "DTSTART;TZID=Europe/Moscow:20260928T190000\nDTEND;TZID=Europe/Moscow:20260928T200000\nRRULE:FREQ=DAILY\nEND:VEVENT\nEND:VCALENDAR\n")
        adapter = YandexAdapter("u@ya.ru", "pwd")
        puts = []
        adapter.get = lambda href: (master_ics, '"m1"')
        adapter._request = lambda method, url, body=None, headers=None: (puts.append((method, url, body, headers)) or (201, {"etag": '"m2"'}, ""))
        rec = "2026-09-30T16:00:00+00:00"
        moved = {"master_id": "evt_m", "master_href": "https://caldav.yandex.ru/c/ser1.ics", "recurrence_id": rec, "uid": "ser1", "title": "Серия (перенос)",
                 "start_utc": "2026-09-30T17:00:00+00:00", "end_utc": "2026-09-30T18:00:00+00:00", "tz": "Europe/Moscow", "all_day": 0, "status": "confirmed",
                 "reminders_json": "[]", "attendees_json": "[]"}
        res = adapter.update({}, moved, '"m1"', {})
        method, url, body, headers = puts[-1]
        self.assertEqual((method, headers.get("If-Match"), res["etag"]), ("PUT", '"m1"', '"m2"'))
        self.assertIn("RRULE:FREQ=DAILY", body)                     # master kept
        self.assertIn("RECURRENCE-ID;TZID=Europe/Moscow:20260930T190000", body)
        self.assertIn("SUMMARY:Серия (перенос)", body)
        cancelled = {**moved, "status": "cancelled"}
        adapter.update({}, cancelled, '"m2"', {})
        body2 = puts[-1][2]
        self.assertIn("EXDATE;TZID=Europe/Moscow:20260930T190000", body2)
        self.assertNotIn("RECURRENCE-ID", body2)
