"""Independent review counterexamples against 33b86f8 (tree dcd62978).

Evidence only: no product writes, no network, no accounts. Real calendar
commands/adapters run against the existing conditional in-memory fakes
(FakeCalDAV / FakeGoogle). Each test asserts the CORRECT outcome, so a failure
is the reproduced defect; messages print the observed provider/local state.
"""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

import os
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'skills/calendar'), str(ROOT / 'skills/calendar/tests')]

import test_calendar_review_r5 as r5  # noqa: E402
from test_calendar_provider_divergence import (caldav_context, add_google_calendar, FakeGoogle,  # noqa: E402
                                               _day, run_open_intents)
from test_calendar_atomic_replay import instance  # noqa: E402
from providers import ics_to_rows  # noqa: E402
from providers_google import GoogleAdapter  # noqa: E402
from model import iso_utc, now_utc  # noqa: E402
from scripts.worker import reconcile_calendar  # noqa: E402
import ops  # noqa: E402
import tools  # noqa: E402


def provider_slots(ctx, cid, server, href):
    """Occurrences the CalDAV server itself would show for one resource."""
    text, etag = server.resources[href]
    rows = ics_to_rows(text, cid, href, etag, ctx.tz)
    master = dict(next(r for r in rows if not r.get('recurrence_id')), id='provider')
    excs = [dict(r, id='x' + r['recurrence_id'], master_id='provider') for r in rows if r.get('recurrence_id')]
    occ = ops.expand([master], _day(0), _day(30), lambda _: excs, ctx.tz)
    return sorted(o['occurrence_start_utc'] for o in occ)


def local_slots(ctx, eid):
    master = ctx.store.get_event(eid)
    occ = ops.expand([master], _day(0), _day(30), ctx.store.exceptions_for, ctx.tz)
    return sorted(o['occurrence_start_utc'] for o in occ)


def series(ctx, cid, count=5):
    out = json.loads(tools.cal_create(ctx, title='Series', start=_day(2).isoformat(),
                                      rrule=f'FREQ=DAILY;COUNT={count}', calendars=[cid], confirm=True))
    assert out['status'] == 'created', out
    return out['event']['id']


def linked_google_primary():
    """r5.linked() with the calendars reversed: Google holds the primary, CalDAV the copy."""
    ctx, left, server = caldav_context()
    right = add_google_calendar(ctx.store)
    google = FakeGoogle()
    gadapter = google.attach(GoogleAdapter('owner@example.test', 'token', 'refresh',
                                           now_utc() + timedelta(days=1), 'client', '', None))
    caldav = ctx.providers.adapter_for('')

    class Routing:
        def adapter_for(self, account):
            return gadapter if account.startswith('google:') else caldav
    ctx.providers = Routing()
    out = json.loads(tools.cal_create(ctx, title='Before', start=_day(2).isoformat(),
                                      calendars=[right, left], confirm=True))
    assert out['status'] == 'created', out
    src = out['event']['id']
    copy = next(r for r in ctx.store.group_masters(ctx.store.get_event(src)['link_group_id']) if r['calendar_id'] == left)
    return ctx, right, left, server, google, gadapter, caldav, src, copy


class Review33bCounterexamples(unittest.TestCase):
    # ── A. Owner delete-following truncation (serializer still infers only from master/live ICS) ──
    def test_A1_delete_following_keeps_prior_single_cancellation_on_caldav(self):
        ctx, cid, server = caldav_context()
        eid = series(ctx, cid)
        href = ctx.store.get_event(eid)['href']
        out = json.loads(tools.cal_delete(ctx, id=f'{eid}@{iso_utc(_day(3))}', scope='this', confirm=True))
        self.assertEqual(out['status'], 'deleted', out)
        self.assertNotIn(iso_utc(_day(3)), provider_slots(ctx, cid, server, href))   # sanity: accepted at CalDAV
        out = json.loads(tools.cal_delete(ctx, id=f'{eid}@{iso_utc(_day(5))}', scope='following', confirm=True))
        self.assertEqual(out['status'], 'deleted', out)
        # A later sync does not repair it: the master ETag equals our own ACK, so the feed skips it.
        reconcile_calendar(ctx.store, ctx.providers, ctx.providers.adapter_for(''), ctx.store.get_calendar(cid), ctx.tz)
        local, remote = local_slots(ctx, eid), provider_slots(ctx, cid, server, href)
        self.assertEqual(remote, local, f'local(after sync)={local}\nprovider={remote}\n'
                         f'resource EXDATE lines={[l for l in server.resources[href][0].splitlines() if l.startswith("EXDATE")]}')

    def test_A2_linked_copy_delete_following_keeps_cascaded_cancellation_on_caldav(self):
        ctx, cid, caldav, google, left, eid, copy = r5.ReviewR5Tests().linked()
        instance(ctx, google, copy['id'], 3)
        out = json.loads(tools.cal_delete(ctx, id=f"{copy['id']}@{iso_utc(_day(3))}", scope='this', confirm=True))
        self.assertEqual(out['status'], 'deleted', out)
        out = json.loads(tools.cal_delete(ctx, id=f"{copy['id']}@{iso_utc(_day(5))}", scope='following', confirm=True))
        self.assertIn(out['status'], ('deleted', 'pending'), out)
        run_open_intents(ctx, 3)
        href = ctx.store.get_event(eid)['href']
        local, remote = local_slots(ctx, eid), provider_slots(ctx, cid, caldav, href)
        self.assertEqual(remote, local, f'local={local}\nprovider={remote}')

    # ── B. Cancellation still pending when a rule-changing write is staged ──
    def test_B1_offline_cancel_then_following_split_keeps_cancellation(self):
        ctx, cid, server = caldav_context()
        eid = series(ctx, cid)
        href = ctx.store.get_event(eid)['href']
        server.down = True
        out = json.loads(tools.cal_delete(ctx, id=f'{eid}@{iso_utc(_day(3))}', scope='this', confirm=True))
        self.assertEqual(out['status'], 'pending', out)
        out = json.loads(tools.cal_update(ctx, id=f'{eid}@{iso_utc(_day(4))}', title='Later', scope='following', confirm=True))
        self.assertEqual(out['status'], 'pending', out)
        old_master_intent = [json.loads(i['payload_json']) for i in ctx.store.intents_for_event(eid)
                             if i['kind'] == 'update'][-1]
        server.down = False
        run_open_intents(ctx, 6)
        self.assertEqual(ctx.store.open_intents(), [], ctx.store.open_intents())
        local, remote = local_slots(ctx, eid), provider_slots(ctx, cid, server, href)
        self.assertEqual(remote, local, f'local={local}\nprovider={remote}\n'
                         f"staged cancelled_slots={old_master_intent.get('cancelled_slots')} "
                         f"prior_intent_ids={old_master_intent.get('prior_intent_ids')}")

    def test_B2_offline_cancel_then_rule_growth_keeps_cancellation(self):
        ctx, cid, server = caldav_context()
        eid = series(ctx, cid)
        href = ctx.store.get_event(eid)['href']
        server.down = True
        out = json.loads(tools.cal_delete(ctx, id=f'{eid}@{iso_utc(_day(3))}', scope='this', confirm=True))
        self.assertEqual(out['status'], 'pending', out)
        out = json.loads(tools.cal_update(ctx, id=eid, rrule='FREQ=DAILY;COUNT=7', scope='all', confirm=True))
        self.assertEqual(out['status'], 'pending', out)
        server.down = False
        run_open_intents(ctx, 6)     # companion order: rowid; the cancellation was queued first
        self.assertEqual(ctx.store.open_intents(), [], ctx.store.open_intents())
        local, remote = local_slots(ctx, eid), provider_slots(ctx, cid, server, href)
        self.assertEqual(remote, local, f'local={local}\nprovider={remote}')

    # ── C. Linked Google source confirmed cancelled: still a DELETE of the same resource ──
    def test_C1_linked_google_source_cancelled_in_incremental_feed_retires_without_conflict(self):
        ctx, right, left, server, google, gadapter, caldav, src, copy = linked_google_primary()
        ext = ctx.store.get_event(src)['external_id']
        google.events[ext].update(status='cancelled', etag=google._etag())   # another client deleted it
        reconcile_calendar(ctx.store, ctx.providers, gadapter, ctx.store.get_calendar(right), ctx.tz, 'sync-cursor')
        run_open_intents(ctx, 3)
        row = ctx.store.get_event(src)
        self.assertEqual(ctx.store.open_intents(), [], {'source_row': row and {k: row[k] for k in ('sync_state', 'deleted_at')},
                                                         'open': [(i['kind'], i['state'], i['result_json']) for i in ctx.store.open_intents()]})
        self.assertFalse(server.resources, 'CalDAV copy should be deleted')

    def test_C2_linked_google_source_absent_from_full_feed_retires_without_conflict(self):
        ctx, right, left, server, google, gadapter, caldav, src, copy = linked_google_primary()
        ext = ctx.store.get_event(src)['external_id']
        google.events[ext].update(status='cancelled', etag=google._etag())
        reconcile_calendar(ctx.store, ctx.providers, gadapter, ctx.store.get_calendar(right), ctx.tz, '')
        run_open_intents(ctx, 3)
        row = ctx.store.get_event(src)
        self.assertEqual(ctx.store.open_intents(), [], {'source_row': row and {k: row[k] for k in ('sync_state', 'deleted_at')},
                                                         'open': [(i['kind'], i['state'], i['result_json']) for i in ctx.store.open_intents()]})

    # ── D. A provider-imported override on a linked copy is never in the other member's sync basis ──
    def _imported_override(self):
        ctx, cid, caldav, google, left, eid, copy = r5.ReviewR5Tests().linked()
        gadapter = ctx.providers.adapter_for('google:owner@example.test')
        ext, _ = instance(ctx, google, copy['id'], 4)
        google.events[ext].update(summary='Moved at Google', start={'dateTime': (_day(4) + timedelta(hours=2)).isoformat()},
                                  end={'dateTime': (_day(4) + timedelta(hours=3)).isoformat()}, etag=google._etag())
        reconcile_calendar(ctx.store, ctx.providers, gadapter, ctx.store.get_calendar(copy['calendar_id']), ctx.tz)
        run_open_intents(ctx, 3)
        imported = ctx.store.exceptions_for(copy['id'])
        self.assertEqual(len(imported), 1, imported)
        return ctx, cid, caldav, google, left, eid, copy, imported[0]

    def test_D1_caldav_source_cancellation_retires_after_copy_has_imported_override(self):
        ctx, cid, caldav, google, left, eid, copy, imported = self._imported_override()
        caldav.edit(ctx.store.get_event(eid)['href'],
                    lambda c: next(x for x in c.walk() if x.name == 'VEVENT' and not x.get('RECURRENCE-ID')).__setitem__('STATUS', 'CANCELLED'))
        for _ in range(2):
            reconcile_calendar(ctx.store, ctx.providers, left, ctx.store.get_calendar(cid), ctx.tz)
        run_open_intents(ctx, 3)
        self.assertTrue(ctx.store.get_event(eid)['deleted_at'],
                        f"source still live; imported override link_group_id={imported['link_group_id']!r}, "
                        f"in CalDAV basis={imported['id'] in ctx.store.sync_basis(cid)}")
        self.assertEqual(google.events[copy['external_id']]['status'], 'cancelled')

    def test_D2_caldav_source_title_reaches_copy_after_copy_has_imported_override(self):
        ctx, cid, caldav, google, left, eid, copy, imported = self._imported_override()
        def retitle(c):
            next(x for x in c.walk() if x.name == 'VEVENT' and not x.get('RECURRENCE-ID'))['SUMMARY'] = 'After'
        caldav.edit(ctx.store.get_event(eid)['href'], retitle)
        reconcile_calendar(ctx.store, ctx.providers, left, ctx.store.get_calendar(cid), ctx.tz)
        run_open_intents(ctx, 3)
        self.assertEqual(google.events[copy['external_id']]['summary'], 'After',
                         f"imported override link_group_id={imported['link_group_id']!r}")


if __name__ == '__main__':
    unittest.main()
