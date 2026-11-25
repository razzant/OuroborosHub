"""calendar_worker — the one long-lived companion of the calendar skill.

Host contract (extension_companion.py): cwd = payload dir, env carries
OUROBOROS_SKILL_STATE_DIR, granted env_from_settings values,
HOST_SERVICE_URL / HOST_SERVICE_TOKEN and PYTHONPATH with the isolated deps.

Every tick (60 s): health file → provider sync (each account every SYNC_INTERVAL
or at once after `sync_now`) → retry of due intents → plan + deliver reminders.
No LLM, no scheduler of its own, no second store.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from datetime import timedelta
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.dirname(HERE)
if PAYLOAD not in sys.path:
    sys.path.insert(0, PAYLOAD)

import ops  # noqa: E402
import links  # noqa: E402
import reminders as rem  # noqa: E402
import google_host  # noqa: E402
from host_sign_in import HostSignIn  # noqa: E402
from model import PROVIDER_GOOGLE, PROVIDER_LOCAL, CALDAV_PROVIDERS, SCOPE_ALL, SECRET_KEYS, get_tz, iso_utc, now_utc, own_reminders, parse_stored, civil_in_basis, has_recurrence, held_series, instance_slot  # noqa: E402
from providers import Providers, sync_window  # noqa: E402
from store import Store  # noqa: E402

TICK_SEC = 60
SYNC_INTERVAL_SEC = 180
# Calendar 2.1.0 and earlier accepted Google sync tokens after skipping instances whose series the calendar
# does not hold, and such a token never returns an unchanged skipped instance. Tokens are therefore kept
# under this kind; a token of the older kind is replaced once by an ordinary full listing.
GOOGLE_CURSOR_KIND = "google_sync_token_instances"
_stop = False


def _log(msg: str, **fields: Any) -> None:
    rec = {"ts": iso_utc(now_utc()), "msg": msg, **fields}
    print(json.dumps(rec, ensure_ascii=False), flush=True)


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    _log("stopping", signal=signum)


def write_health(state_dir: str, payload: Dict[str, Any]) -> None:
    path = os.path.join(state_dir, "companion_health.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"ts": iso_utc(now_utc()), "pid": os.getpid(), **payload}, fh, ensure_ascii=False)
    os.replace(tmp, path)


def deps_ok() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for mod in ("icalendar", "dateutil", "cryptography"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "ok")
        except Exception as exc:
            out[mod] = f"missing: {exc}"
    return out


# ── sync ────────────────────────────────────────────────────────────

def sync_account(store: Store, providers: Providers, account: Dict[str, Any], tz) -> Dict[str, Any]:
    adapter = providers.adapter_for(account["id"])
    if adapter is None:
        # The companion only sees the secrets it was spawned with; a fresh key needs a skill restart, so do not
        # overwrite a status the child may have just set — report and move on.
        reason = (getattr(providers, "errors", {}) or {}).get(account["id"]) or "секрет недоступен фоновому процессу (перезапусти скилл после добавления ключа)"
        return {"account": account["id"], "status": "skipped", "reason": reason}
    report: Dict[str, Any] = {"account": account["id"], "calendars": 0, "events": 0, "deleted": 0, "propagated": 0, "resynced": 0}
    try:
        cals = adapter.list_calendars()
        for cal in cals:
            existing = store.get_calendar(cal["id"])
            store.upsert_calendar({**cal, "role_visible": existing["role_visible"] if existing else True,
                                   "role_busy": existing["role_busy"] if existing else True,
                                   "role_publish": existing["role_publish"] if existing else False,
                                   "publish_mode": existing["publish_mode"] if existing else "busy"},
                                  restore=True)  # lifts a disconnect's retirement after sign-in; an owner deletion's receipt keeps it
            stored = store.get_calendar(cal["id"])
            if stored is None or stored.get('deleted_at'):
                continue  # deleted by the owner, or disconnected while discovery was in flight
            report["calendars"] += 1
            state = store.get_sync_state(cal["id"])
            ctag = cal.get("ctag") or ""
            window_start, window_end = map(iso_utc, sync_window(now_utc()))
            if (ctag and state.get("cursor") == ctag and state.get("cursor_kind") == "ctag"
                    and state.get("window_start") == window_start and state.get("window_end") == window_end):
                continue  # unchanged server AND unchanged query: a moving horizon still needs a fetch
            cursor = state.get("cursor") if state.get("cursor_kind") == GOOGLE_CURSOR_KIND else ""
            try:
                n, d, p, next_cursor, kind = reconcile_calendar(store, providers, adapter, stored, tz, cursor)
            except ops.ProviderError as exc:
                if exc.kind != "gone":
                    raise
                # 410 invalidates the cursor, not event identity or queued owner work.
                wipe_external_cache(store, stored["id"])
                report["resynced"] += 1
                n, d, p, next_cursor, kind = reconcile_calendar(store, providers, adapter, stored, tz, "")
            if kind == 'disconnected':
                continue  # The observation belongs to a connection withdrawn during I/O.
            report["events"] += n
            report["deleted"] += d
            report["propagated"] += p
            bad = list(getattr(adapter, "parse_errors", []) or [])
            parse_note = f"не разобрано событий: {len(bad)} ({'; '.join(bad[:3])})"[:300] if bad else ""
            if bad:
                report.setdefault("parse_errors", []).extend(bad[:10])
            if kind == "google_sync_token":
                store.set_sync_state(cal["id"], cursor=next_cursor, cursor_kind=GOOGLE_CURSOR_KIND, last_ok_at=iso_utc(now_utc()), last_error=parse_note)
            else:
                store.set_sync_state(cal["id"], cursor=ctag if next_cursor is not None else '', cursor_kind="ctag" if ctag else "window",
                                     window_start=window_start, window_end=window_end, last_ok_at=iso_utc(now_utc()), last_error=parse_note)
        parse_total = len(report.get("parse_errors") or [])
        store.set_account_status(account["id"], "ok", f"не разобрано событий: {parse_total}" if parse_total else "", synced=True)
    except ops.ProviderError as exc:
        status = "auth_failed" if exc.kind == "auth" else "error"
        store.set_account_status(account["id"], status, exc.message)
        report["status"] = status
        report["error"] = exc.message
        return report
    report["status"] = "ok"
    return report


def wipe_external_cache(store: Store, calendar_id: str) -> None:
    """Legacy name: invalidate only the cursor; the full feed reconciles stable IDs."""
    store.set_sync_state(calendar_id, cursor="", cursor_kind="", last_error="resync after 410")


def reconcile_calendar(store: Store, providers: Providers, adapter, cal: Dict[str, Any], tz, cursor: str = ""):
    """Server → local rows. Only the chosen source changes its following projections.

    Returns (upserts, deletions, propagations, next_cursor, cursor_kind). For Google the cancelled ids in an
    incremental feed ARE the confirmed deletions; for CalDAV a window read plus HEAD 404 on the known href is.
    """
    connection = store.calendar_connection(cal['id'])
    if connection is None:
        return 0, 0, 0, None, 'disconnected'
    basis = store.sync_basis(cal['id'])
    rows, next_cursor, kind, extra = adapter.fetch(cal, cursor, tz=tz)
    awaiting = _awaiting_identity(store, adapter, cal)
    cancelled = frozenset(extra) if kind == 'google_sync_token' else frozenset()
    received = {}
    if kind == 'google_sync_token':
        if not cursor:
            listed_ids = {row['external_id'] for row in rows} | cancelled
            parents = {row['external_id'] for row in rows if has_recurrence(row) and row.get('status') != 'cancelled'}
            # The bounded listing alone proves nothing. A joined date still has its own address,
            # so its absence is resolved the same way as when it was a standalone record.
            cancelled |= frozenset(r['external_id'] for r in basis.values()
                                   if r['calendar_id'] == cal['id'] and r['master_id'] and r['kept_master'] == r['master_id']
                                   and r['external_id'] and r['external_id'] not in listed_ids
                                   and not r['deleted_at'] and r['status'] != 'cancelled'
                                   and basis.get(r['master_id'], {}).get('external_id') in parents
                                   and not adapter.exists_event(cal, r['external_id']))
        for row in rows:
            if row.get('master_external_id') and row.get('recurrence_id') and row.get('status') != 'cancelled':
                received.setdefault(row['master_external_id'], []).append(row)
    result = ops._finish_command(store, providers, _stage_feed, 'companion',
                                 cal, rows, basis, awaiting, kind, next_cursor, connection, cancelled, received)
    if result.get('disconnected'):
        return 0, 0, 0, None, 'disconnected'
    upserts, deleted, propagated = result['upserts'], result['deleted'], result['propagated']
    next_cursor = result['next_cursor']
    retry_required = result['retry_required']
    if result.get('temporal_gaps'):
        adapter.parse_errors = list(getattr(adapter, 'parse_errors', []) or []) + result['temporal_gaps']
    explicit = {r['external_id'] for r in rows} | cancelled
    def confirmed(local):
        nonlocal retry_required, upserts
        keep, absent = received.get(local['external_id'], []), []
        if kind == 'google_sync_token' and has_recurrence(local) and not store.delete_requested(local['id']):
            keep, absent = _parent_date_observations(adapter, cal, local, basis, keep, explicit)
        if local['id'] in basis and (keep or absent) and not store.delete_requested(local['id']):
            result = ops._finish_command(store, providers, _stage_absent_cascade, 'companion', local['id'],
                                         basis[local['id']], basis, connection, keep, absent)
            count = int(result['status'] == 'ok')
            upserts += result.get('kept', 0)
            retry_required |= result.get('stale', False)
        else:
            count = _confirmed_deletion(store, providers, local, basis.get(local['id']), basis, connection) if local['id'] in basis else 0
        if not count:
            retry_required = True
        return count
    incremental = bool(cursor) and kind == 'google_sync_token'
    if kind == "google_sync_token":
        # Google: cancelled ids in the (incremental) feed are the confirmed deletions (roast Fable F7).
        # A held instance record settled its cancellation in the feed stage and stays as a hidden record.
        retiring = {r['id'] for r in basis.values() if r['calendar_id'] == cal['id']
                    and r['external_id'] in cancelled and has_recurrence(r)}
        handled_with_parent = {r['external_id'] for r in basis.values() if r['calendar_id'] == cal['id']
                               and (r['master_id'] or r['kept_master']) in retiring}
        for ext_id in extra:
            if ext_id in result['instances'] or ext_id in handled_with_parent:
                continue
            existing = store.find_by_external(cal["id"], external_id=ext_id)
            if existing is not None and not existing.get("deleted_at"):
                deleted += confirmed(existing)
    if not incremental and kind == "google_sync_token":
        # The initial feed is time-bounded: absence alone is not a deletion.
        seen = {r.get("external_id") for r in rows} | set(extra)
        # Series whose dates this listing still brings: an undecided absence of one of them is checked again.
        with store._conn() as c:
            known = [dict(r) for r in c.execute("SELECT * FROM events WHERE calendar_id=? AND master_id='' AND external_id!='' AND deleted_at IS NULL", (cal["id"],))]
        for local in known:
            if local["external_id"] in seen or (local.get("sync_state") in ("pending", "conflict", "failed") and not store.delete_requested(local["id"])):
                continue
            if held_series(local) and local.get("status") == "cancelled":
                continue  # a held cancellation is kept for its series; a listing or lookup without it does not undo it
            try:
                if adapter.exists_event(cal, local["external_id"]):
                    continue
            except ops.ProviderError as exc:
                if has_recurrence(local) or held_series(local):
                    if exc.kind in ('auth', 'forbidden'):
                        raise  # an access failure has not decided whether the series or received date is missing
                    retry_required = True
                continue
            deleted += confirmed(local)
    if not incremental and kind != "google_sync_token":
        # CalDAV window: rows inside the window whose resource is gone from the server.
        seen_hrefs = set(extra)
        for local in store.window(now_utc() - timedelta(days=30), now_utc() + timedelta(days=400), [cal["id"]], include_hidden=True, include_masters=True):
            if local.get("master_id") or not local.get("href") or local["href"] in seen_hrefs:
                continue
            if local.get("sync_state") in ("pending", "conflict") and not store.delete_requested(local["id"]):
                continue
            try:
                still_there = adapter.exists(local["href"])
            except ops.ProviderError:
                continue  # an access problem is not a deletion
            if still_there:
                continue
            deleted += confirmed(local)
    if not store.connection_is_current(connection):
        return upserts, deleted, propagated, None, 'disconnected'
    if kind == "google_sync_token":
        # After the read's deletions, a date cancelled in this read can only join as cancelled.
        gaps = _attach_held_instances(store, cal["id"], connection)
        if gaps:
            adapter.parse_errors = list(getattr(adapter, 'parse_errors', []) or []) + gaps
    if retry_required:
        next_cursor = '' if kind == 'google_sync_token' else None
    return upserts, deleted, propagated, next_cursor, kind


def _parent_date_observations(adapter, calendar, master, basis, received, explicit):
    """Only days independently received as instances have an identity to check after their parent leaves."""
    kept, absent = list(received), []
    live_ids = {r['external_id'] for r in received}
    for event in basis.values():
        eid = event['external_id']
        if (not eid or event['deleted_at'] or event['status'] == 'cancelled' or event['calendar_id'] != calendar['id']
                or (event['master_id'] or event['kept_master']) != master['id']):
            continue
        if not event['kept_master']:
            continue  # an ordinary override is not a separately received date: it retires with its series (2.1.0)
        observed = event
        if eid not in explicit:
            try:
                current = adapter.read_current(calendar, event)
            except ops.ProviderError as exc:
                if exc.kind not in ('not_found', 'gone'):
                    raise  # neither an access failure nor an outage is a confirmed absence
                observed = None
            else:
                observed = current.get('event')
                if current.get('cancelled') or (observed or {}).get('status') == 'cancelled':
                    observed = None
                elif (not observed or observed.get('external_id') != eid
                      or observed.get('master_external_id') != master['external_id'] or not observed.get('recurrence_id')):
                    raise ops.ProviderError('parse', 'Google did not identify the retained series date')
                if observed is not None:
                    kept.append(observed)
        if observed is None or (eid in explicit and eid not in live_ids):
            absent.append(event['id'])
    return kept, absent


def _stage_feed(store, cal, rows, basis, awaiting, kind, next_cursor, connection, cancelled=frozenset(), received=None):
    """Commit accepted source snapshots and their copy intents together, with no network.

    A failure anywhere before commit retains the old source ETag, so the next
    fetch can replay the observation. After commit, durable intents own delivery.
    """
    if not store.connection_is_current(connection):
        return {'disconnected': True, 'assignments': []}
    protected_masters = set()
    retry_required = False
    upserts = deleted = propagated = 0
    assignments = []
    temporal_gaps = []
    masters: Dict[str, str] = {}
    copy_commands = {}  # all parts of this feed for one destination stage together
    refreshed: Dict[str, str] = {}   # CalDAV master id → UID, for series whose whole resource was re-read this pass
    for row in rows:
        if row.get("recurrence_id"):
            continue
        existing = store.find_by_external(cal["id"], external_id=row["external_id"], href=row["href"], uid=row["uid"])
        claimed = awaiting.get(row["external_id"]) or awaiting.get(row["href"])
        if claimed is not None and (existing is None or existing["id"] == claimed["id"]):
            # The resource a lost create wrote: it is that local row, never a second imported copy.
            existing = claimed
            if row.get("status") != "cancelled" and store.adopt_created_identity(
                    claimed["id"], {"external_id": row["external_id"], "href": row["href"], "etag": row["etag"]}):
                existing = {**claimed, "external_id": row["external_id"], "href": row["href"]}
        if row.get("status") == "cancelled":
            if kind == 'google_sync_token' and row['external_id'] in cancelled:
                continue  # settle the master and its separately observed dates together after this feed
            if existing is not None:
                if existing.get("sync_state") == "pending_delete" or store.delete_requested(existing["id"]):
                    deleted += int(store.confirm_pending_delete(existing["id"], basis[existing['id']])) if existing['id'] in basis else 0
                elif existing.get("sync_state") not in ("pending", "conflict", "failed"):
                    if existing['id'] in basis:
                        result = _stage_absent_cascade(store, existing['id'], basis[existing['id']], basis, connection,
                                                       (received or {}).get(existing['external_id'], ()))
                        deleted += int(result.get('status') == 'ok')
                        upserts += result.get('kept', 0)
                        retry_required |= result.get('status') != 'ok' or result.get('stale', False)
                        assignments.extend(result.get('assignments', []))
                if not (store.get_event(existing['id']) or {}).get('deleted_at'):
                    retry_required = True
            continue
        if existing is None:
            saved = store.insert_provider_master({k: v for k, v in row.items() if k != "master_external_id"})
            if saved is None:
                next_cursor = ''
                continue
            masters[row["uid"]] = saved["id"]
            upserts += 1
            continue
        masters[row["uid"]] = existing["id"]
        if existing.get("sync_state") in ("pending", "conflict", "pending_delete", "failed"):
            protected_masters.add(existing['id'])
            next_cursor = ''
            continue  # our write is in flight; the intent path settles it
        if row.get('time_kind') == 'floating':
            if not existing.get('tz') and not existing.get('time_kind') and any(
                    row.get(key) != existing.get(key) for key in ('start_utc', 'end_utc')):
                # The old parser retained no interpretation zone. Do not guess
                # one or rebase its authored/pending recurrence addresses.
                protected_masters.add(existing['id'])
                next_cursor = ''
                retry_required = True
                temporal_gaps.append('Legacy floating event ' + existing['id'] + ' has no recorded parsing zone; stored times and pending work were retained.')
                continue
        row = civil_in_basis(row, existing)
        if existing.get("etag") == row["etag"] and existing.get("sync_state") == "synced" and not existing.get("deleted_at"):
            # Our acknowledged wire format may use UTC for a zoned one-off.
            # Equal ETags permit parser enrichment, not replacing its authoring zone.
            enrichment = {}
            if row.get('time_kind') and row['time_kind'] != existing.get('time_kind'):
                enrichment['time_kind'] = row['time_kind']
                if not existing.get('tz') and row.get('tz'):
                    enrichment['tz'] = row['tz']
            if row.get('status') and row['status'] != existing.get('status'):
                enrichment['status'] = row['status']
            if row.get('raw_payload') and row['raw_payload'] != existing.get('raw_payload'):
                enrichment['raw_payload'] = row['raw_payload']
            if enrichment:
                if existing['id'] not in basis or not store.apply_provider_snapshot(existing['id'], enrichment, observed=basis[existing['id']]):
                    protected_masters.add(existing['id'])
                    next_cursor = ''
                    continue
                upserts += 1
            if kind != 'google_sync_token' and has_recurrence(row):
                refreshed[existing['id']] = row['uid']
            continue
        changes = {k: row[k] for k in ("title", "description", "location", "start_utc", "end_utc", "tz", "time_kind", "all_day", "rrule", "exdates", "rdates",
                                        "status", "organizer", "attendees_json", "my_response", "etag", "raw_payload") if k in row}
        if existing.get("origin") == "external":
            changes["reminders_json"] = _imported_reminders(existing, row["reminders_json"], cal["provider"], bool((store.get_setting(rem.MODE_KEY) or {}).get(cal["id"])))
            changes["availability"] = row.get("availability") or existing.get("availability")
        dates = (_kept_dates_in_series(store, existing['id'])
                 if kind == 'google_sync_token' and ops._series_delta(existing, changes) is not None else [])
        if dates:
            with store._conn() as c:
                if any(date['id'] not in basis or not store._same_observation(date, basis[date['id']])
                       or store._unsettled_owner_write(c, date['id'], date['sync_state']) for date in dates):
                    protected_masters.add(existing['id'])
                    next_cursor = ''
                    continue  # one motion may not separate the new master from its still-editing date
        if existing['id'] not in basis or not store.apply_provider_snapshot(existing["id"], {**changes, "sync_state": "synced"}, observed=basis[existing['id']]):
            protected_masters.add(existing['id'])
            next_cursor = ''
            continue
        links.observe_projection(store, existing, row)
        upserts += 1
        if kind != "google_sync_token" and has_recurrence(row):
            refreshed[existing["id"]] = row["uid"]
        _queue_source_delta(store, existing, row, copy_commands, preserve_slots=[e['recurrence_id'] for e in dates])
        if kind == 'google_sync_token':
            _readdress_kept_dates(store, existing, row, received or {}, copy_commands, basis)
    instances = set()   # Google instance IDs handled without the root-deletion path
    sparse = []
    if kind == 'google_sync_token':
        unresolved = cancelled - {r['external_id'] for r in rows}
        for previous in store.overrides_by_external(cal['id'], unresolved):
            parent = store.get_event(previous['master_id'])
            if parent and parent.get('external_id'):
                sparse.append({**previous, 'master_external_id': parent['external_id'], 'status': 'cancelled',
                               '_addressed_cancellation': True})
                instances.add(previous['external_id'])
    for row in [*rows, *sparse]:
        if not row.get("recurrence_id"):
            continue
        if kind == "google_sync_token" and row.get("external_id"):
            # A Google instance names its series exactly; the iCalUID it shares is not that series.
            # An instance that already has its own record keeps it; the record joins the series below.
            series = row.get("master_external_id") or ""
            master = store.find_by_external(cal["id"], external_id=series) if series else None
            held = store.find_by_external(cal["id"], external_id=row["external_id"])
            live = master is not None and not master.get("deleted_at")
            if live and series in cancelled:
                continue   # the retiring series' transaction owns the fate of all its observed dates
            if held is not None or not live:
                instances.add(row["external_id"])
                outcome = _stage_held_instance(store, cal, row, held, basis, copy_commands)
                if outcome == "protected":
                    next_cursor = ''
                elif outcome == "changed":
                    upserts += 1
                continue
            if series in cancelled:
                continue   # its series leaves in this read; a date still received is kept after that deletion
            master_id = master["id"]
        else:
            master_id = masters.get(row["uid"])
            if not master_id:
                master = store.find_by_external(cal["id"], external_id=row.get("master_external_id") or "", uid=row["uid"])
                master_id = master["id"] if master else ""
        if not master_id:
            continue
        if master_id in protected_masters:
            continue
        master_row = store.get_event(master_id)
        if not row.get('_addressed_cancellation'):
            row = civil_in_basis(row, master_row or {})
        if row['recurrence_id'] in ops.excluded_slots(master_row or {}):
            continue  # a stale instance cannot undo the master's explicit EXDATE
        exceptions = store.exceptions_for(master_id)
        exc = next((e for e in exceptions if e.get("recurrence_id") == row["recurrence_id"]), None)
        # Google has one stable external identity per exception; older imports
        # could file that same instance under the owner's then-current zone.
        same_identity = [e for e in exceptions if kind == 'google_sync_token' and row.get('external_id')
                         and e.get('external_id') == row['external_id']]
        if exc is None and same_identity:
            exc = min(same_identity, key=lambda e: (e.get('created_at') or '', e['id']))
        retained = next((e for e in same_identity if e.get('kept_master') == master_id), None)
        if retained is not None:
            # The independently received date owns this ID even when its stored slot moved.
            outcome = _stage_held_instance(store, cal, row, retained, basis, copy_commands)
            if outcome == 'protected':
                next_cursor = ''
            elif outcome == 'changed':
                upserts += 1
            continue
        payload = {k: v for k, v in row.items() if k != "master_external_id"}
        payload.update({"master_id": master_id})
        if master_row:
            payload.update({key: master_row.get(key) for key in ('link_group_id', 'is_primary', 'link_state')})
        status_enrichment = bool(exc and exc.get('etag') == row['etag'] and row.get('status') and exc.get('status') != row['status'])
        native_enrichment = bool(exc and row.get('raw_payload') and row['raw_payload'] != exc.get('raw_payload'))
        changed = exc is None or exc.get("etag") != row["etag"] or exc.get('recurrence_id') != row['recurrence_id'] or ('time_kind' in row and exc.get('time_kind', '') != row['time_kind']) or status_enrichment or native_enrichment
        same_status = {'status': row['status']} if status_enrichment and row['status'] != 'cancelled' else {}
        materially = exc is None or links.changed({**exc, **same_status}, row, provider_observation=True)
        if exc is None:
            if kind == 'google_sync_token' and master_row.get('visibility') == 'hidden':
                payload['visibility'] = 'hidden'   # only an explicit owner edit makes this newly seen day visible
            saved = store.insert_provider_exception(master_id, payload)
            if saved is None:
                next_cursor = ''
                continue   # the slot was taken by an owner write, or the series closed, since we looked: the next pass decides
            observed_id = saved['id']
        elif changed:
            if exc.get("sync_state") in ("pending", "conflict", "pending_delete", "failed"):
                if exc.get('recurrence_id') != row['recurrence_id']:
                    temporal_gaps.append('Provider instance ' + exc['id'] + ' retains an older recurrence address while its owner write is unsettled.')
                    next_cursor = ''
                continue   # our own write to this occurrence is in flight
            fields = {k: payload[k] for k in ("title", "description", "location", "start_utc", "end_utc", "status", "etag",
                                              "attendees_json", "my_response", "tz", "time_kind", "all_day", "recurrence_id", "raw_payload") if k in payload}
            if row.get('status') == 'cancelled' and kind == 'google_sync_token':
                # The provider may give only the address of the cancelled slot.
                fields = {'status': 'cancelled', 'etag': row.get('etag') or exc.get('etag') or '',
                          'raw_payload': _merged_native(exc, row), 'recurrence_id': row['recurrence_id']}
            elif exc.get("origin") == "external":
                fields['availability'] = payload.get('availability') or exc.get('availability')
                fields["reminders_json"] = _imported_reminders(exc, payload.get("reminders_json") or "[]", cal["provider"], bool((store.get_setting(rem.MODE_KEY) or {}).get(cal["id"])))
            if exc['id'] not in basis or not store.apply_provider_snapshot(exc["id"], fields, observed=basis[exc['id']]):
                next_cursor = ''
                continue
            _carry_readdressed_notices(store, master_id, exc.get('recurrence_id'), row['recurrence_id'])
            observed_id = exc['id']
        else:
            observed_id = exc['id']
        for duplicate in same_identity:
            if duplicate['id'] != observed_id and duplicate['id'] in basis:
                if store.retire_synced_exception(duplicate['id'], observed=basis[duplicate['id']]):
                    _carry_readdressed_notices(store, master_id, duplicate.get('recurrence_id'), row['recurrence_id'])
                else:
                    temporal_gaps.append('Duplicate provider instance ' + duplicate['id'] + ' was retained because its owner write is unsettled.')
                    next_cursor = ''
        upserts += 1
        if changed and materially:   # a mere resource etag bump (any PUT of the series) is not a change of this occurrence
            master_row = store.get_event(master_id)
            if master_row and master_row.get('link_group_id') and not links.source_is(store, master_row):
                store.update_event(master_id, {'link_state': links.DIVERGED})
                child = next((event for event in store.exceptions_for(master_id) if event['recurrence_id'] == row['recurrence_id']), None)
                if child:
                    store.update_event(child['id'], {'link_state': links.DIVERGED})
            if master_row and master_row.get("link_group_id") and links.source_is(store, master_row):
                key = row["recurrence_id"]
                for sib in links.followers(store, master_row):
                    if str(row.get("status") or "") == "cancelled":
                        copy_commands.setdefault(sib['id'], []).append(('delete', f"{sib['id']}@{key}", {}, 'this'))
                    else:
                        projected = links.projection_changes(store, row, sib)
                        for field in ('rrule', 'rdates', 'exdates'):
                            projected.pop(field, None)
                        copy_commands.setdefault(sib['id'], []).append(('update', f"{sib['id']}@{key}", projected, 'this'))
    for master_id, uid in refreshed.items():
        # A CalDAV series is one resource read whole: an override VEVENT the server no longer holds (its date
        # cancelled by EXDATE, or the move undone) is not a local exception any more. Our own unsent writes stay.
        master = store.get_event(master_id) or {}
        present = {civil_in_basis(r, master)["recurrence_id"] for r in rows if r.get("recurrence_id") and r.get("uid") == uid}
        feed_master = next((row for row in rows if row.get('uid') == uid and not row.get('recurrence_id')), {})
        excluded = ops.excluded_slots(master) | ops.excluded_slots(civil_in_basis(feed_master, master))
        for exc in store.exceptions_for(master_id):
            if exc.get('status') == 'cancelled' and exc.get('recurrence_id') in excluded:
                continue  # CalDAV confirms a cancelled slot with EXDATE, not an override VEVENT.
            if exc.get("recurrence_id") not in present and exc['id'] in basis:
                canonical = civil_in_basis(exc, master).get('recurrence_id')
                if store.retire_synced_exception(exc["id"], observed=basis[exc['id']]):
                    if canonical in present:
                        _carry_readdressed_notices(store, master_id, exc.get('recurrence_id'), canonical)
                elif canonical in present and canonical != exc.get('recurrence_id'):
                    temporal_gaps.append('Duplicate provider instance ' + exc['id'] + ' retains unsettled owner work.')
                    next_cursor = ''
    for root, commands in copy_commands.items():
        result = _stage_sync_copy(store, basis, root, commands)
        propagated += int(result.get('status') == 'ok')
        retry_required |= result.get('status') != 'ok'
        _bind_source_intents(store, result.get('assignments', []), connection)
        assignments.extend(result.get('assignments', []))
        if result.get('message'):
            temporal_gaps.append('Linked copy ' + root + ' not propagated: ' + result['message'])
    return {'upserts': upserts, 'deleted': deleted, 'propagated': propagated, 'instances': instances,
            'next_cursor': next_cursor, 'assignments': assignments, 'retry_required': retry_required, 'temporal_gaps': temporal_gaps}


def _queue_source_delta(store, existing, row, copy_commands, preserve_slots=()):
    """Queue a chosen source's observed change for its following copies."""
    if not (existing.get("link_group_id") and links.source_is(store, existing)):
        return
    # One observation is one complete master change. Splitting time,
    # title and exclusions into separate sends loses the later parts
    # as soon as the first send becomes pending during an outage.
    delta = {k: row.get(k) or '' for k in links.COPY_FIELDS
             if k in row and existing.get(k) != row.get(k)}
    if 'start_utc' in delta:
        # Provider dates are absolute, even when unchanged beside a new anchor.
        delta.update({key: row.get(key) or '' for key in ('exdates', 'rdates')})
    for sib in links.followers(store, existing):
        changes = {k: v for k, v in delta.items() if k not in ('title', 'description', 'location') or sib.get('publish_mode') != 'busy'}
        if changes:
            copy_commands.setdefault(sib['id'], []).append(('update', sib['id'], changes, 'all', preserve_slots))


def _readdress_kept_dates(store, previous, incoming, received, copy_commands, basis):
    """Move the series address, not a separately received day's effective time."""
    delta = ops._series_delta(previous, incoming)
    if delta is None:
        return
    master = store.get_event(previous['id'])
    latest = {r['external_id']: r for r in received.get(previous['external_id'], ())}
    dates = _kept_dates_in_series(store, master['id'])
    occupied = {}
    for e in [*store.exceptions_for(master['id']), *dates]:
        occupied.setdefault(e['recurrence_id'], set()).add(e['id'])
    moves = {}
    for date in dates:
        old = date['recurrence_id']
        anchor = parse_stored(old)
        if anchor is None:
            continue
        shifted = iso_utc(ops.shift_recurrence_time(master, anchor, delta))
        new = instance_slot(latest[date['external_id']], master) if date['external_id'] in latest else shifted
        if not ops.series_date(master, new):
            new = shifted if ops.series_date(master, shifted) else old
        if new != old and date['id'] in basis and store._same_observation(date, basis[date['id']]):
            moves[date['id']] = new
    # A slot can be occupied now and vacated by this very observation. Eliminate only collisions in the final set.
    from collections import Counter
    counts = Counter(moves.values())
    while blocked := [eid for eid, slot in moves.items() if counts[slot] > 1 or occupied.get(slot, set()).difference(moves)]:
        for eid in blocked:
            del moves[eid]
    for date in dates:
        if date['id'] in moves:
            store.drop_reminders_for_slot(master['id'], date['recurrence_id'])
    for date in dates:
        new = moves.get(date['id'])
        if new:
            store.update_event(date['id'], {'recurrence_id': new})  # the enclosing feed transaction owns the set
            if date['master_id']:
                ops.carry_date_notices(store, master['id'], date['recurrence_id'], master['id'], new, date)
            date = store.get_event(date['id'])
            basis[date['id']] = date  # only our own address change; the same feed may still carry the day's content
        _queue_kept_slot(store, date, master, date, copy_commands, ops._withdrawn(date))


def _kept_dates_in_series(store, master_id):
    return [e for e in store.exceptions_for(master_id) if e['kept_master'] == master_id] + store.kept_dates([master_id])


def _stage_held_instance(store, cal, row, held, basis, copy_commands):
    """Keep one Google instance whose series this calendar does not hold (or not yet joined) as its own record.

    No other date of the series is generated for it. A cancellation keeps the record hidden with its content, so a
    series that arrives later cannot bring the slot back; it settles the owner's own delete of that date, removes
    the date's own copies, and cancels the date in the copies of its waiting ``master``. Returns 'changed', 'same'
    or 'protected'.
    """
    payload = {k: v for k, v in row.items() if k != "master_external_id"}
    cancelled = str(row.get("status") or "") == "cancelled"
    if held is None:
        return "changed" if store.insert_provider_instance(payload) else "protected"
    if held["id"] not in basis:
        return "protected"   # it changed after this read began: the next read replays this observation
    if not cancelled and held.get('status') == 'cancelled' and store.owner_cancelled_received_date(held['id']):
        return 'same'   # a new organizer version does not undo a confirmed owner deletion of that instance
    if held.get("deleted_at") and (cancelled or store.delete_requested(held['id'])
                                  or not row.get('etag') or row['etag'] == held.get('etag')):
        return "same"   # an owner delete or a replay is not a new invitation
    if cancelled and (held.get("sync_state") == "pending_delete" or store.delete_requested(held["id"])):
        # Google applied the owner's delete, perhaps without its answer reaching us: the Store's rules settle it.
        if not store.confirm_pending_delete(held["id"], basis[held["id"]]):
            return "protected"
        _queue_kept_slot(store, held, ops.kept_series_of(store, held), held, copy_commands, True)
        return "changed"
    row = civil_in_basis(row, held)
    if held.get("kept_master"):
        row = {k: v for k, v in row.items() if k != "recurrence_id"}   # bound: the slot it holds in its series is local
    if cancelled:
        if held.get("status") == "cancelled" and held.get("etag") == row.get("etag"):
            return "same"
        # Google's cancelled instance carries only its identity and original slot; the record keeps its content.
        fields = {"status": "cancelled", "etag": row.get("etag") or held.get("etag") or "",
                  "raw_payload": _merged_native(held, row), "sync_state": "synced"}
    elif held.get("etag") == row["etag"] and held.get("sync_state") == "synced":
        fields = {k: row[k] for k in ("status", "raw_payload") if row.get(k) and row[k] != held.get(k)}
        if not fields:
            return "same"
    else:
        fields = {k: row[k] for k in ("title", "description", "location", "start_utc", "end_utc", "tz", "time_kind", "all_day", "recurrence_id",
                                      "status", "organizer", "attendees_json", "my_response", "etag", "raw_payload") if k in row}
        if held.get("origin") == "external":
            fields["reminders_json"] = _imported_reminders(held, row["reminders_json"], cal["provider"], bool((store.get_setting(rem.MODE_KEY) or {}).get(cal["id"])))
            fields["availability"] = row.get("availability") or held.get("availability")
        fields["sync_state"] = "synced"
    released, commands = _split_slot_release(store, held) if cancelled and not held.get('master_id') else (False, ())
    if released:
        fields['kept_master'] = ''
    if not store.apply_provider_snapshot(held["id"], fields, observed=basis[held["id"]]):
        return "protected"
    for member, command in commands:
        copy_commands.setdefault(member['id'], []).append(command)
    current = store.get_event(held["id"]) or {**held, **fields}
    meaningful = (cancelled and not released) or (
        not cancelled and (held.get('deleted_at') or links.changed(held, row, provider_observation=True)))
    if cancelled:
        if not held.get('master_id') and held.get("link_group_id") and links.source_is(store, held):
            for sib in links.followers(store, held):
                copy_commands.setdefault(sib['id'], []).append(('delete', sib['id'], {}, 'all'))
    else:
        if not held.get('master_id'):
            links.observe_projection(store, held, row)
            if not held.get('deleted_at'):
                _queue_source_delta(store, held, row, copy_commands)
    if meaningful:   # an ETag-only or native-only observation re-sends nothing to the series' copies
        _queue_kept_slot(store, current, ops.kept_series_of(store, current), current, copy_commands, cancelled)
    return "changed"


def _split_slot_release(store, date):
    """An old Google identity disappearing after a split is not a cancellation of the new series' slot."""
    master = ops.kept_series_of(store, date)
    if not date.get('kept_master') or not master or master['external_id'] == held_series(date):
        return False, ()
    key = date['recurrence_id']
    own = ops.series_date(master, key)
    return True, [(sib, ('slot', f"{sib['id']}@{key}", own, 'this'))
                  for sib in links.followers(store, master)] if own is not None else ()


def _merged_native(held, row):
    """A sparse native observation over the retained native object: identity, slot and status change, content stays."""
    try:
        previous = json.loads(held.get("raw_payload") or "{}") or {}
        incoming = json.loads(row.get("raw_payload") or "{}") or {}
    except (TypeError, ValueError):
        return row.get("raw_payload") or held.get("raw_payload") or ""
    if not isinstance(previous, dict) or not isinstance(incoming, dict):
        return row.get("raw_payload") or ""
    return json.dumps({**previous, **incoming}, ensure_ascii=False)


def _queue_kept_slot(store, record, master, row, copy_commands, cancelled):
    """A waiting kept date's observed change reaches the copies of its series that carry its slot (ops.kept_slot_commands)."""
    for sib_id, command in ops.kept_slot_commands(store, record, master, row, cancelled):
        copy_commands.setdefault(sib_id, []).append(command)


_KEPT_CONTENT = ("title", "description", "location", "start_utc", "end_utc", "tz", "time_kind", "all_day", "status", "visibility",
                 "organizer", "attendees_json", "my_response", "availability", "reminders_json", "etag", "raw_payload")


def _keep_orphaned_instances(store, cal, master, rows, basis, copy_commands):
    """A date this read still brings while its series left in it (cancelled, or confirmed absent from a full listing)
    stays one meeting: it is kept as its own record, with the notices already delivered for that date. The stored
    override went with its series through the guarded deletion; nothing is detached from a live series.

    Returns (records and their old slots, stale): when that override changed after this read began (an owner edit the provider
    already accepted), the record keeps the override's newer content and the next read is a full listing.
    """
    kept, stale = [], False
    for row in rows:
        with store._conn() as c:
            active = store.find_by_external(cal['id'], external_id=row['external_id'])
            if active and not active.get('deleted_at'):
                outcome = _stage_held_instance(store, cal, row, active, basis, copy_commands)
                stale |= outcome == 'protected'
                active = store.get_event(active['id'])
                kept.append((active, ops.kept_slot(active, master)))
                continue
            retired = c.execute("SELECT * FROM events WHERE master_id=? AND external_id=? ORDER BY deleted_at DESC LIMIT 1",
                                (master["id"], row["external_id"])).fetchone()
        payload = {k: v for k, v in row.items() if k != "master_external_id"}
        before = basis.get(retired["id"]) if retired else None
        if retired is not None and store.owner_cancelled_received_date(retired['id']):
            continue
        if retired is not None and retired['etag'] != row['etag'] and (
                before is None or any(retired[k] != before.get(k) for k in _KEPT_CONTENT)):
            payload.update({k: retired[k] for k in _KEPT_CONTENT})
            stale = True
        else:
            previous = before or master   # a temporary override from this feed is not an owner's visibility choice
            mode = bool((store.get_setting(rem.MODE_KEY) or {}).get(cal['id']))
            payload['reminders_json'] = _imported_reminders(previous, row['reminders_json'], cal['provider'], mode)
            payload['visibility'] = previous['visibility']
            payload['availability'] = row.get('availability') or previous['availability']
        if before and before['kept_master'] == master['id'] and not active:
            # This date already has an owner-facing address. Keep it across the retirement of its series.
            store.update_event(before['id'], {**payload, 'master_id': '', 'kept_master': '', 'link_group_id': '',
                                             'is_primary': True, 'link_state': links.FOLLOWING, 'deleted_at': None})
            saved = store.get_event(before['id'])
        else:
            saved = store.insert_provider_instance(payload)
        if saved is None:
            continue
        slot = retired["recurrence_id"] if retired else ops.kept_slot(saved, master)
        kept.append((saved, slot))
        if slot:
            ops.carry_date_notices(store, master['id'], slot, saved['id'], '', saved)
    return kept, stale


def _keep_existing_followers(view, siblings, kept):
    """Keep only copies on destinations that actually carried this date before the series left."""
    assignments, predecessors = [], {}
    if not kept or not siblings:
        return assignments, predecessors
    exceptions = {m['id']: {e['recurrence_id']: e for e in view.exceptions_for(m['id'])} for m in siblings}
    for date, slot in kept:
        if not slot or date.get('status') == 'cancelled' or view.delete_requested(date['id']):
            continue
        own = ops.kept_copy_calendars(view, date)
        carried_here = []
        for member in siblings:
            if slot in ops.excluded_slots(member):
                continue
            exc = exceptions[member['id']].get(slot)
            if (exc and exc['status'] != 'cancelled') or (not exc and ops.series_date(member, slot)):
                carried_here.append(member['calendar_id'])
        if not set(carried_here) - own:
            continue
        result = ops._stage_reassign_event(view, date, [date['calendar_id'], *sorted(own), *carried_here], scope='this')
        for assignment in result.get('added', []):
            predecessors.setdefault(assignment['calendar_id'], []).extend(assignment.get('_intent_ids', []))
            assignments.append(assignment)
    return assignments, predecessors


def _attach_held_instances(store, calendar_id, connection=None):
    """Join received date records to their series once this calendar stores it.

    Only the exact recurringEventId selects the series the first time; the record then stays bound to it
    (``kept_master``). The record becomes that series' override in place, and a notice already delivered for it
    stays delivered under its new series key. One that cannot join (removed, its own copies, unsettled owner work)
    is bound with its slot and waits; the series reads that slot as the record's (ops.waiting_instances).
    """
    gaps = []
    candidates = store.held_instances(calendar_id, include_removed='unbound')
    for candidate, _, _ in ops.held_with_series(store, candidates):
        with store.transaction() as view:
            if connection and not view.connection_is_current(connection):
                break
            record = view.get_event(candidate['id'])
            master = ops.kept_series_of(view, record) if record and not record.get('master_id') else None
            if master is None or master['calendar_id'] != calendar_id:
                continue
            slot = {**civil_in_basis(record, master), 'recurrence_id': ops.kept_slot(record, master)}
            reason = view.attach_instance(record['id'], master['id'], fields=slot)
            if not reason:
                ops.carry_date_notices(view, record['id'], '', master['id'], slot['recurrence_id'], slot)
                view.drop_reminders_for(record['id'])
            elif reason == 'slot_taken':
                gaps.append(f"Google instance {record['id']} and a stored date of its series {master['id']} name the same slot; both are kept.")
    return gaps


def _carry_readdressed_notices(store, master_id, before, after):
    """A repaired original-slot address must not resend a settled notice for the same effective meeting."""
    if not before or not after or before == after:
        return
    for previous in store.settled_reminders_for_split(master_id, before):
        if previous.get('recurrence_id') == before:
            effective = previous['occurrence_start_utc']
            store.carry_settled_reminder(previous, master_id, effective, after, previous['fire_at_utc'],
                                        rem.notice_id_for(master_id, effective, previous['offset_min'], after))


def _awaiting_identity(store: Store, adapter, cal: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """external_id/href a create of each identity-less local row would have written → that row."""
    created_identity = getattr(adapter, "created_identity", None)
    if not callable(created_identity):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for local in store.events_awaiting_identity(cal["id"]):
        try:
            identity = created_identity(cal, local)
        except ops.ProviderError:
            continue
        for key in (identity.get("external_id"), identity.get("href")):
            if key:
                out[key] = local
    return out


def _imported_reminders(local: Dict[str, Any], incoming: str, provider: str, mode_on: bool = False) -> str:
    """A provider alarm muted by calendar-owned delivery is not an instruction to silence Ouroboros.

    In this mode an empty explicit Google alarm set is also indistinguishable from an owner edit
    disabling alerts at Google; the local selected rule wins until a new positive offset arrives.
    Outside the mode, Google useDefault remains authoritative, including a return to defaults.
    CalDAV has no explicit default-vs-none bit, so retain local off on an alarm-free feed.
    """
    local_value = own_reminders(local.get("reminders_json"))
    incoming_value = own_reminders(incoming)
    if provider == PROVIDER_GOOGLE and mode_on and incoming_value == []:
        return str(local.get("reminders_json") or "[]")
    if provider in CALDAV_PROVIDERS and incoming_value is None and (mode_on or local_value == []):
        # CalDAV has no useDefault bit. In calendar-owned mode the host itself mutes VALARM;
        # its subsequent echo must not erase a selected local offset (or an explicit off).
        return str(local.get("reminders_json") or "[]")
    return incoming


def _copy_unchanged(view, event_id, basis):
    rows = [view.get_event(event_id)] + view.exceptions_for(event_id)
    with view._conn() as c:
        for row in rows:
            before = basis.get(row['id']) if row else None
            if before is None or row.get('link_state') in (links.DIVERGED, links.MISSING) or view._unsettled_owner_write(c, row['id'], row['sync_state']):
                return False
            # Own ACK bookkeeping can change between propagation parts; a
            # different owner edit must still win over the old observation.
            semantic = {k:v for k,v in before.items() if k not in ('etag','updated_at','sync_state','raw_payload')}
            if not view._same_observation(row, semantic):
                return False
    return True


def _stage_sync_copy(view, basis, root, commands):
    """A divergent copy cannot roll back the source feed or another copy.

    Roll back this copy's whole command batch, not only its failing suffix.
    Keep the provider snapshot and expose the rejected propagation for inspection.
    """
    with view._conn() as c:
        c.execute('SAVEPOINT calendar_copy')
        try:
            result = _stage_sync_copy_commands(view, basis, root, commands)
        except ValueError as exc:
            c.execute('ROLLBACK TO calendar_copy')
            row = view.get_event(root)
            if row:
                view.update_event(root, {'link_state': links.DIVERGED})
                view.set_sync_state(row['calendar_id'], last_error='Linked copy not propagated: ' + str(exc))
            result = {'status': 'protected', 'assignments': [], 'message': str(exc)}
        finally:
            c.execute('RELEASE calendar_copy')
    return result


def _stage_sync_copy_commands(view, basis, root, commands):
    """Check the pre-fetch destination once and stage the entire observation.

    No provider I/O happens until all its parts and their dependencies commit.
    Pending protection from our first part must not reject our own later parts.
    """
    if not _copy_unchanged(view, root, basis):
        return {'status': 'protected', 'assignments': []}
    assignments, previous = [], None
    for kind, address, changes, scope, *slots in commands:
        if kind == 'slot':   # a series date given back to a following copy (ops.stage_series_slot)
            member_id, key = address.rsplit('@', 1)
            result = ops.stage_series_slot(view, view.get_event(member_id), key, changes)
        else:
            result = (ops._stage_update_event(view, address, changes, scope=scope, propagate=False, from_source=True,
                                              preserve_slots=slots[0] if slots else ())
                      if kind == 'update' else ops._stage_delete_event(view, address, scope=scope, cascade=False, from_source=True))
        for assignment in result.get('assignments', []):
            for iid in assignment.get('_intent_ids', []):
                intent = view.get_intent(iid)
                payload = json.loads(intent['payload_json'])
                if previous:
                    payload.setdefault('after_intent_id', previous)
                    if payload.get('baseline'):
                        observed = ops._baseline_before(basis, view.get_event(intent['event_id']))
                        if observed and links.changed(observed, payload['baseline']):
                            # An instance may inherit the acknowledged master change or
                            # keep its own prior content. Keep both addressed facts.
                            payload['baseline_after_predecessor'] = payload['baseline']
                            payload['baseline'] = observed
                    with view._conn() as c:
                        c.execute('UPDATE intents SET payload_json=? WHERE id=?', (json.dumps(payload), iid))
                previous = iid
            assignments.append(assignment)
    return {'status': 'ok', 'assignments': assignments}


def _bind_source_intents(store, assignments, connection):
    """The staged observation's connection owns propagation, even if a link later chooses another source."""
    with store._conn() as c:
        for assignment in assignments:
            for iid in assignment.get('_intent_ids', []):
                intent = store.get_intent(iid)
                payload = json.loads(intent['payload_json'])
                payload['source_connection'] = connection
                c.execute('UPDATE intents SET payload_json=? WHERE id=?', (json.dumps(payload), iid))


def _confirmed_deletion(store: Store, providers: Providers, local: Dict[str, Any], observed=None, basis=None, connection=None) -> int:
    """The provider answered that this resource is gone; apply it only if no accepted owner write says otherwise *now*.

    ``local`` is the caller's earlier snapshot: it names the row and the identity the answer was about, nothing more.
    The provider call already happened outside SQLite; every protection check is repeated inside the write transaction."""
    connection = connection or store.calendar_connection(local['calendar_id'])
    if not store.connection_is_current(connection):
        return 0
    if local.get("sync_state") == "pending_delete" or store.delete_requested(local["id"]):
        # This row was selected for deletion by an earlier scoped operation.
        # Its siblings already have their own intents if the owner requested a cascade.
        return int(store.confirm_pending_delete(local["id"], observed))
    observed = observed if observed is not None else local
    basis = basis if basis is not None else store.sync_basis(local['calendar_id'])
    staged = ops._finish_command(store, providers, _stage_absent_cascade, "companion", local["id"], observed, basis, connection)
    return int(staged.get("status") == "ok")


def _stage_absent_cascade(view, event_id: str, observed: Dict[str, Any], basis, connection=None, still_received=(), absent_dates=()) -> Dict[str, Any]:
    """Runs inside ops._finish_command's write transaction: the guard and the linked-copy cascade commit together or not at all."""
    row = view.get_event(event_id)
    connection = connection or (view.calendar_connection(row['calendar_id']) if row else None)
    if not view.connection_is_current(connection) or view.provider_absence_blocker(event_id, observed):
        return {"status": "protected", "assignments": []}
    was_source = links.source_is(view, row)
    siblings = links.followers(view, row)
    if any(not _copy_unchanged(view, sibling['id'], basis)
           for sibling in siblings):
        return {'status': 'protected', 'assignments': []}
    for eid in absent_dates:
        if view.provider_absence_blocker(eid, basis[eid]):
            return {'status': 'protected', 'assignments': []}
        if not basis[eid]['master_id'] and any(not _copy_unchanged(view, sibling['id'], basis)
                                               for sibling in links.followers(view, basis[eid])):
            return {'status': 'protected', 'assignments': []}
    # Every caller already has provider-confirmed absence. Retire that
    # observation locally; only the still-existing copies need remote writes.
    if not view.retire_provider_absent(event_id, observed):
        return {'status': 'protected', 'assignments': []}
    assignments = []
    for eid in absent_dates:
        if not basis[eid]['master_id']:   # attached dates were retired with the series
            removed = _stage_absent_cascade(view, eid, basis[eid], basis, connection)
            assignments.extend(removed['assignments'])
    copy_commands = {}
    kept, stale = _keep_orphaned_instances(view, view.get_calendar(row['calendar_id']), row, still_received, basis, copy_commands) if still_received else ([], False)
    for root, commands in copy_commands.items():
        result = _stage_sync_copy(view, basis, root, commands)
        stale |= result.get('status') != 'ok'
        assignments.extend(result.get('assignments', []))
    if row.get('link_group_id') and not was_source:
        view.update_event(event_id, {'link_state': links.MISSING})
    kept_copies, replacements = _keep_existing_followers(view, siblings, kept)
    assignments.extend(kept_copies)
    for sibling in siblings:
        removed = ops._stage_delete_event(view, sibling['id'], scope=SCOPE_ALL, cascade=False, from_source=True).get('assignments', [])
        ops._after_all(view, removed, replacements.get(sibling['calendar_id'], []))
        assignments.extend(removed)
    released, commands = _split_slot_release(view, row) if held_series(row) else (False, ())
    if released:
        view.update_event(event_id, {'kept_master': ''})
        for copy, command in commands:
            changed = _stage_sync_copy(view, basis, copy['id'], [command])
            assignments.extend(changed.get('assignments', []))
    elif held_series(row):
        # A received date Google removed: the following copies of its series stop carrying it (independent ones keep theirs).
        assignments.extend(ops._stage_kept_slot(view, {**row, 'status': 'cancelled'}))
    _bind_source_intents(view, assignments, connection)
    return {'status': 'ok', 'assignments': assignments, 'kept': len(kept), 'stale': stale}


# ── main loop ───────────────────────────────────────────────────────

def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    state_dir = os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ""
    if not state_dir:
        _log("no OUROBOROS_SKILL_STATE_DIR; exiting")
        return 2
    store = Store(state_dir)
    secrets = {k: os.environ.get(k, "") for k in SECRET_KEYS}
    providers = Providers(secrets, state_dir)
    channel = rem.NotifyChannel()
    deps = deps_ok()
    secrets_present = {k: bool(v) for k, v in secrets.items()}
    _log("companion started", state_dir=state_dir, deps=deps, secrets_present=secrets_present, host_service=bool(os.environ.get("HOST_SERVICE_URL")))
    last_sync: Dict[str, float] = {}
    tick = 0
    handed_over = None
    while not _stop:
        tick += 1
        tz = get_tz(store.get_setting("timezone") or "")
        health: Dict[str, Any] = {"deps": deps, "secrets_present": secrets_present, "tick": tick, "sync": [], "intents": None, "reminders": None, "channel": None}
        try:
            # reminders first: a slow provider must not delay a due notice
            channel.state(refresh=(tick % 10 == 1 or channel.state() != "ready"))
            stats = rem.deliver_due(store, channel, tz)
            sign_in = HostSignIn()
            handed_over, signed_in = google_host.worker_pass(store, secrets, state_dir, sign_in, handed_over, _log)
            if signed_in:   # only on an Ouroboros that holds sign-ins
                health['sign_in'] = signed_in
            providers = Providers(secrets, state_dir, host=sign_in)  # reconnect/disconnect applies on the next pass, not after a process restart
            requested = parse_stored(store.get_setting("sync_requested_at") or "")
            for account in store.list_accounts():
                if account['provider'] == PROVIDER_LOCAL:
                    continue
                due = time.time() - last_sync.get(account["id"], 0) >= SYNC_INTERVAL_SEC
                forced = requested is not None and (requested.timestamp() > last_sync.get(account["id"], 0))
                if due or forced:
                    try:
                        health["sync"].append(sync_account(store, providers, account, tz))
                    except Exception as exc:
                        health["sync"].append({'account': account['id'], 'status': 'error',
                                              'error': f'{type(exc).__name__}: {exc}'})
                        _log('account sync failed', account=account['id'], error=str(exc))
                    finally:
                        last_sync[account["id"]] = time.time()
            health["intents"] = ops.retry_due_intents(store, providers, owner="companion")
            held: Dict[str, str] = {}
            planned = rem.plan(store, lambda s, e: ops.expand_window(store, s, e, owner_tz=tz, strict=True, failed=held), held=held)
            health["reminders"] = {"planned": planned, "held_series": held, **stats}
            health["channel"] = channel.state()
            store.set_setting("notify_channel_state", {"state": health["channel"], "checked_at": iso_utc(now_utc())})
        except Exception as exc:
            health["error"] = f"{type(exc).__name__}: {exc}"
            _log("tick error", error=health["error"], trace=traceback.format_exc()[-800:])
        try:
            write_health(state_dir, health)
        except OSError as exc:
            _log("health write failed", error=str(exc))
        for _ in range(TICK_SEC):
            if _stop:
                break
            time.sleep(1)
    _log("companion stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
