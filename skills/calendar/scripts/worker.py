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
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.dirname(HERE)
if PAYLOAD not in sys.path:
    sys.path.insert(0, PAYLOAD)

import ops  # noqa: E402
import reminders as rem  # noqa: E402
from model import PROVIDER_GOOGLE, PROVIDER_YANDEX, SCOPE_ALL, SECRET_KEYS, get_tz, iso_utc, now_utc, own_reminders, parse_stored  # noqa: E402
from providers import Providers  # noqa: E402
from store import Store  # noqa: E402

TICK_SEC = 60
SYNC_INTERVAL_SEC = 180
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
                                   "publish_mode": existing["publish_mode"] if existing else "busy"})
            report["calendars"] += 1
            stored = store.get_calendar(cal["id"])
            state = store.get_sync_state(cal["id"])
            ctag = cal.get("ctag") or ""
            if ctag and state.get("cursor") == ctag and state.get("cursor_kind") == "ctag" and state.get("last_ok_at"):
                continue  # CalDAV: nothing changed on the server since the last pass
            cursor = state.get("cursor") if state.get("cursor_kind") == "google_sync_token" else ""
            try:
                n, d, p, next_cursor, kind = reconcile_calendar(store, providers, adapter, stored, tz, cursor)
            except ops.ProviderError as exc:
                if exc.kind != "gone":
                    raise
                # 410 invalidates the cursor, not event identity or queued owner work.
                wipe_external_cache(store, stored["id"])
                report["resynced"] += 1
                n, d, p, next_cursor, kind = reconcile_calendar(store, providers, adapter, stored, tz, "")
            report["events"] += n
            report["deleted"] += d
            report["propagated"] += p
            bad = list(getattr(adapter, "parse_errors", []) or [])
            parse_note = f"не разобрано событий: {len(bad)} ({'; '.join(bad[:3])})"[:300] if bad else ""
            if bad:
                report.setdefault("parse_errors", []).extend(bad[:10])
            if kind == "google_sync_token":
                store.set_sync_state(cal["id"], cursor=next_cursor, cursor_kind=kind, last_ok_at=iso_utc(now_utc()), last_error=parse_note)
            else:
                store.set_sync_state(cal["id"], cursor=ctag if next_cursor is not None else '', cursor_kind="ctag" if ctag else "window", last_ok_at=iso_utc(now_utc()), last_error=parse_note)
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
    """Server → local rows. Our own linked copies get external edits propagated; confirmed deletions cascade (28 A).

    Returns (upserts, deletions, propagations, next_cursor, cursor_kind). For Google the cancelled ids in an
    incremental feed ARE the confirmed deletions; for CalDAV a window read plus HEAD 404 on the known href is.
    """
    basis = store.sync_basis(cal['id'])
    rows, next_cursor, kind, extra = adapter.fetch(cal, cursor, tz=tz)
    awaiting = _awaiting_identity(store, adapter, cal)
    result = ops._finish_command(store, providers, _stage_feed, 'companion',
                                 cal, rows, basis, awaiting, kind, next_cursor)
    upserts, deleted, propagated = result['upserts'], result['deleted'], result['propagated']
    next_cursor = result['next_cursor']
    retry_required = result['retry_required']
    def confirmed(local):
        nonlocal retry_required
        count = _confirmed_deletion(store, providers, local, basis.get(local['id']), basis) if local['id'] in basis else 0
        if not count:
            retry_required = True
        return count
    incremental = bool(cursor) and kind == 'google_sync_token'
    if kind == "google_sync_token":
        # Google: cancelled ids in the (incremental) feed are the confirmed deletions (roast Fable F7).
        for ext_id in extra:
            existing = store.find_by_external(cal["id"], external_id=ext_id)
            if existing is not None and not existing.get("deleted_at"):
                deleted += confirmed(existing)
    if not incremental and kind == "google_sync_token":
        # The initial feed is time-bounded: absence alone is not a deletion.
        seen = {r.get("external_id") for r in rows} | set(extra)
        with store._conn() as c:
            known = [dict(r) for r in c.execute("SELECT * FROM events WHERE calendar_id=? AND master_id='' AND external_id!='' AND deleted_at IS NULL", (cal["id"],))]
        for local in known:
            if local["external_id"] in seen or (local.get("sync_state") in ("pending", "conflict", "failed") and not store.delete_requested(local["id"])):
                continue
            try:
                if adapter.exists_event(cal, local["external_id"]):
                    continue
            except ops.ProviderError:
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
    if retry_required:
        next_cursor = '' if kind == 'google_sync_token' else None
    return upserts, deleted, propagated, next_cursor, kind


def _stage_feed(store, cal, rows, basis, awaiting, kind, next_cursor):
    """Commit accepted source snapshots and their copy intents together, with no network.

    A failure anywhere before commit retains the old source ETag, so the next
    fetch can replay the observation. After commit, durable intents own delivery.
    """
    protected_masters = set()
    retry_required = False
    upserts = deleted = propagated = 0
    assignments = []
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
            if existing is not None:
                if existing.get("sync_state") == "pending_delete" or store.delete_requested(existing["id"]):
                    deleted += int(store.confirm_pending_delete(existing["id"], basis[existing['id']])) if existing['id'] in basis else 0
                elif existing.get("sync_state") not in ("pending", "conflict", "failed"):
                    if existing['id'] in basis:
                        result = _stage_absent_cascade(store, existing['id'], basis[existing['id']], basis)
                        deleted += int(result.get('status') == 'ok')
                        retry_required |= result.get('status') != 'ok'
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
        if existing.get("etag") == row["etag"] and existing.get("sync_state") == "synced" and not existing.get("deleted_at"):
            continue
        if existing.get("sync_state") in ("pending", "conflict", "pending_delete", "failed"):
            protected_masters.add(existing['id'])
            next_cursor = ''
            continue  # our write is in flight; the intent path settles it
        changes = {k: row[k] for k in ("title", "description", "location", "start_utc", "end_utc", "tz", "all_day", "rrule", "exdates", "rdates",
                                        "status", "organizer", "attendees_json", "my_response", "etag", "raw_payload") if k in row}
        if existing.get("origin") == "external":
            changes["reminders_json"] = _imported_reminders(existing, row["reminders_json"], cal["provider"], bool((store.get_setting(rem.MODE_KEY) or {}).get(cal["id"])))
            changes["availability"] = row.get("availability") or existing.get("availability")
        if existing['id'] not in basis or not store.apply_provider_snapshot(existing["id"], {**changes, "sync_state": "synced"}, observed=basis[existing['id']]):
            protected_masters.add(existing['id'])
            next_cursor = ''
            continue
        upserts += 1
        if kind != "google_sync_token" and row.get("rrule"):
            refreshed[existing["id"]] = row["uid"]
        if existing.get("link_group_id"):
            # One observation is one complete master change. Splitting time,
            # title and exclusions into separate sends loses the later parts
            # as soon as the first send becomes pending during an outage.
            delta = {k: row.get(k) or '' for k in ('start_utc', 'end_utc', 'tz', 'all_day',
                     'rrule', 'rdates', 'exdates', 'title') if existing.get(k) != row.get(k)}
            if 'start_utc' in delta:
                # Provider dates are absolute, even when unchanged beside a new anchor.
                delta.update({key: row.get(key) or '' for key in ('exdates', 'rdates')})
            for sib in store.group_masters(existing['link_group_id']):
                if sib['id'] == existing['id']:
                    continue
                changes = {k: v for k, v in delta.items() if k != 'title' or sib.get('publish_mode') != 'busy'}
                if changes:
                    copy_commands.setdefault(sib['id'], []).append(('update', sib['id'], changes, 'all'))
    for row in rows:
        if not row.get("recurrence_id"):
            continue
        master_id = masters.get(row["uid"])
        if not master_id:
            master = store.find_by_external(cal["id"], external_id=row.get("master_external_id") or "", uid=row["uid"])
            master_id = master["id"] if master else ""
        if not master_id:
            continue
        if master_id in protected_masters:
            continue
        master_row = store.get_event(master_id)
        if row['recurrence_id'] in ops.excluded_slots(master_row or {}):
            continue  # a stale instance cannot undo the master's explicit EXDATE
        exc = next((e for e in store.exceptions_for(master_id) if e.get("recurrence_id") == row["recurrence_id"]), None)
        payload = {k: v for k, v in row.items() if k != "master_external_id"}
        payload.update({"master_id": master_id, "raw_payload": ""})
        changed = exc is None or exc.get("etag") != row["etag"]
        materially = exc is None or any(str(exc.get(k) or "") != str(row.get(k) or "") for k in ("start_utc", "end_utc", "status"))
        if exc is None:
            if store.insert_provider_exception(master_id, payload) is None:
                next_cursor = ''
                continue   # the slot was taken by an owner write, or the series closed, since we looked: the next pass decides
        elif changed:
            if exc.get("sync_state") in ("pending", "conflict", "pending_delete", "failed"):
                continue   # our own write to this occurrence is in flight
            fields = {k: payload[k] for k in ("title", "description", "location", "start_utc", "end_utc", "status", "etag",
                                              "attendees_json", "my_response") if k in payload}
            if exc.get("origin") == "external":
                fields["reminders_json"] = _imported_reminders(exc, payload.get("reminders_json") or "[]", cal["provider"], bool((store.get_setting(rem.MODE_KEY) or {}).get(cal["id"])))
            if exc['id'] not in basis or not store.apply_provider_snapshot(exc["id"], fields, observed=basis[exc['id']]):
                next_cursor = ''
                continue
        upserts += 1
        if changed and materially:   # a mere resource etag bump (any PUT of the series) is not a change of this occurrence
            # 6 A / 22: an unambiguous external change of one occurrence follows to the linked copies
            master_row = store.get_event(master_id)
            if master_row and master_row.get("link_group_id"):
                key = row["recurrence_id"]
                for sib in store.group_masters(master_row["link_group_id"]):
                    if sib["id"] == master_id:
                        continue
                    if str(row.get("status") or "") == "cancelled":
                        copy_commands.setdefault(sib['id'], []).append(('delete', f"{sib['id']}@{key}", {}, 'this'))
                    else:
                        moved = {"start_utc": row["start_utc"], "end_utc": row["end_utc"], "all_day": row.get("all_day")}
                        copy_commands.setdefault(sib['id'], []).append(('update', f"{sib['id']}@{key}", moved, 'this'))
    for master_id, uid in refreshed.items():
        # A CalDAV series is one resource read whole: an override VEVENT the server no longer holds (its date
        # cancelled by EXDATE, or the move undone) is not a local exception any more. Our own unsent writes stay.
        present = {r["recurrence_id"] for r in rows if r.get("recurrence_id") and r.get("uid") == uid}
        excluded = ops.excluded_slots(store.get_event(master_id) or {})
        for exc in store.exceptions_for(master_id):
            if exc.get('status') == 'cancelled' and exc.get('recurrence_id') in excluded:
                continue  # CalDAV confirms a cancelled slot with EXDATE, not an override VEVENT.
            if exc.get("recurrence_id") not in present and exc['id'] in basis:
                store.retire_synced_exception(exc["id"], observed=basis[exc['id']])
    for root, commands in copy_commands.items():
        result = _stage_sync_copy(store, basis, root, commands)
        propagated += int(result.get('status') == 'ok')
        retry_required |= result.get('status') != 'ok'
        assignments.extend(result.get('assignments', []))
    return {'upserts': upserts, 'deleted': deleted, 'propagated': propagated,
            'next_cursor': next_cursor, 'assignments': assignments, 'retry_required': retry_required}


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
    if provider == PROVIDER_YANDEX and incoming_value is None and (mode_on or local_value == []):
        # CalDAV has no useDefault bit. In calendar-owned mode the host itself mutes VALARM;
        # its subsequent echo must not erase a selected local offset (or an explicit off).
        return str(local.get("reminders_json") or "[]")
    return incoming


def _copy_unchanged(view, event_id, basis):
    rows = [view.get_event(event_id)] + view.exceptions_for(event_id)
    with view._conn() as c:
        for row in rows:
            before = basis.get(row['id']) if row else None
            if before is None or view._unsettled_owner_write(c, row['id'], row['sync_state']):
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
    for kind, address, changes, scope in commands:
        result = (ops._stage_update_event(view, address, changes, scope=scope, propagate=False)
                  if kind == 'update' else ops._stage_delete_event(view, address, scope=scope, cascade=False))
        for assignment in result.get('assignments', []):
            for iid in assignment.get('_intent_ids', []):
                intent = view.get_intent(iid)
                payload = json.loads(intent['payload_json'])
                if previous and not payload.get('after_intent_id'):
                    payload['after_intent_id'] = previous
                    with view._conn() as c:
                        c.execute('UPDATE intents SET payload_json=? WHERE id=?', (json.dumps(payload), iid))
                previous = iid
            assignments.append(assignment)
    return {'status': 'ok', 'assignments': assignments}


def _confirmed_deletion(store: Store, providers: Providers, local: Dict[str, Any], observed=None, basis=None) -> int:
    """The provider answered that this resource is gone; apply it only if no accepted owner write says otherwise *now*.

    ``local`` is the caller's earlier snapshot: it names the row and the identity the answer was about, nothing more.
    The provider call already happened outside SQLite; every protection check is repeated inside the write transaction."""
    if local.get("sync_state") == "pending_delete" or store.delete_requested(local["id"]):
        # This row was selected for deletion by an earlier scoped operation.
        # Its siblings already have their own intents if the owner requested a cascade.
        return int(store.confirm_pending_delete(local["id"], observed))
    observed = observed if observed is not None else local
    basis = basis if basis is not None else store.sync_basis(local['calendar_id'])
    staged = ops._finish_command(store, providers, _stage_absent_cascade, "companion", local["id"], observed, basis)
    return int(staged.get("status") == "ok")


def _stage_absent_cascade(view, event_id: str, observed: Dict[str, Any], basis) -> Dict[str, Any]:
    """Runs inside ops._finish_command's write transaction: the guard and the linked-copy cascade commit together or not at all."""
    if view.provider_absence_blocker(event_id, observed):
        return {"status": "protected", "assignments": []}
    row = view.get_event(event_id)
    siblings = [sibling for sibling in view.group_masters(row['link_group_id']) if sibling['id'] != event_id] if row.get('link_group_id') else []
    if any(not _copy_unchanged(view, sibling['id'], basis)
           for sibling in siblings):
        return {'status': 'protected', 'assignments': []}
    # Every caller already has provider-confirmed absence. Retire that
    # observation locally; only the still-existing copies need remote writes.
    if not view.retire_provider_absent(event_id, observed):
        return {'status': 'protected', 'assignments': []}
    assignments = []
    for sibling in siblings:
        assignments.extend(ops._stage_delete_event(view, sibling['id'], scope=SCOPE_ALL, cascade=False).get('assignments', []))
    return {'status': 'ok', 'assignments': assignments}


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
    while not _stop:
        tick += 1
        tz = get_tz(store.get_setting("timezone") or "")
        health: Dict[str, Any] = {"deps": deps, "secrets_present": secrets_present, "tick": tick, "sync": [], "intents": None, "reminders": None, "channel": None}
        try:
            # reminders first: a slow provider must not delay a due notice
            channel.state(refresh=(tick % 10 == 1 or channel.state() != "ready"))
            stats = rem.deliver_due(store, channel, tz)
            requested = parse_stored(store.get_setting("sync_requested_at") or "")
            for account in [*store.list_accounts(provider=PROVIDER_YANDEX), *store.list_accounts(provider=PROVIDER_GOOGLE)]:
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
            planned = rem.plan(store, lambda s, e: ops.expand(store.window(s, e, include_hidden=True), s, e,
                                 store.exceptions_for, owner_tz=tz, strict=True, failed=held), held=held)
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
