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
from model import PROVIDER_GOOGLE, PROVIDER_YANDEX, SECRET_KEYS, get_tz, iso_utc, now_utc, own_reminders, parse_stored  # noqa: E402
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
                # 410: only this calendar's external cache and cursor go; local rows, intents and links stay (roast F9).
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
                store.set_sync_state(cal["id"], cursor=ctag, cursor_kind="ctag" if ctag else "window", last_ok_at=iso_utc(now_utc()), last_error=parse_note)
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
    for row in store.window(now_utc() - timedelta(days=3650), now_utc() + timedelta(days=3650), [calendar_id], include_hidden=True, include_masters=True):
        if row.get("origin") == "external" and not row.get("link_group_id"):
            store.delete_event(row["id"], hard=True)
    store.set_sync_state(calendar_id, cursor="", cursor_kind="", last_error="resync after 410")


def reconcile_calendar(store: Store, providers: Providers, adapter, cal: Dict[str, Any], tz, cursor: str = ""):
    """Server → local rows. Our own linked copies get external edits propagated; confirmed deletions cascade (28 A).

    Returns (upserts, deletions, propagations, next_cursor, cursor_kind). For Google the cancelled ids in an
    incremental feed ARE the confirmed deletions; for CalDAV a window read plus HEAD 404 on the known href is.
    """
    rows, next_cursor, kind, extra = adapter.fetch(cal, cursor, tz=tz)
    incremental = bool(cursor) and kind == "google_sync_token"
    upserts = deleted = propagated = 0
    masters: Dict[str, str] = {}
    for row in rows:
        if row.get("recurrence_id"):
            continue
        existing = store.find_by_external(cal["id"], external_id=row["external_id"], href=row["href"], uid=row["uid"])
        if row.get("status") == "cancelled":
            if existing is not None:
                if existing.get("sync_state") == "pending_delete" or store.delete_requested(existing["id"]):
                    deleted += int(store.confirm_pending_delete(existing["id"]))  # settle only the selected copy and its intent
                elif existing.get("sync_state") not in ("pending", "conflict"):
                    deleted += _confirmed_deletion(store, providers, existing)
            continue
        if existing is None:
            saved = store.insert_event({k: v for k, v in row.items() if k != "master_external_id"})
            masters[row["uid"]] = saved["id"]
            upserts += 1
            continue
        masters[row["uid"]] = existing["id"]
        if existing.get("etag") == row["etag"] and existing.get("sync_state") == "synced":
            continue
        if existing.get("sync_state") in ("pending", "conflict", "pending_delete"):
            continue  # our write is in flight; the intent path settles it
        changes = {k: row[k] for k in ("title", "description", "location", "start_utc", "end_utc", "tz", "all_day", "rrule", "exdates", "rdates",
                                        "status", "organizer", "attendees_json", "my_response", "etag", "raw_payload") if k in row}
        if existing.get("origin") == "external":
            changes["reminders_json"] = _imported_reminders(existing, row["reminders_json"], cal["provider"], bool((store.get_setting(rem.MODE_KEY) or {}).get(cal["id"])))
            changes["availability"] = row.get("availability") or existing.get("availability")
        store.update_event(existing["id"], {**changes, "sync_state": "synced"})
        upserts += 1
        if existing.get("link_group_id") and (existing.get("start_utc") != row["start_utc"] or existing.get("end_utc") != row["end_utc"]):
            moved = {"start_utc": row["start_utc"], "end_utc": row["end_utc"], "all_day": row["all_day"]}
            for sib in store.group_masters(existing["link_group_id"]):
                if sib["id"] == existing["id"]:
                    continue
                ops.update_event(store, providers, sib["id"], moved, scope="all", owner="companion", propagate=False)
                propagated += 1
        if existing.get("link_group_id") and (existing.get("rrule") != row.get("rrule") or existing.get("title") != row.get("title")):
            # A source calendar changing recurrence or title must update the
            # linked copies even when DTSTART is unchanged. Busy copies retain
            # their deliberately private title.
            for sib in store.group_masters(existing["link_group_id"]):
                if sib["id"] == existing["id"] or sib.get("sync_state") == "pending_delete":
                    continue
                delta = {}
                if existing.get("rrule") != row.get("rrule"):
                    delta["rrule"] = row.get("rrule") or ""
                if existing.get("title") != row.get("title") and sib.get("publish_mode") != "busy":
                    delta["title"] = row.get("title") or ""
                if delta:
                    result = ops.update_event(store, providers, sib["id"], delta, scope="all", owner="companion", propagate=False)
                    propagated += int(result.get("status") == "ok")
        if existing.get("link_group_id") and row.get("rrule"):
            # a date cancelled on the server as EXDATE (CalDAV style) is a cancelled occurrence for the copies too
            new_ex = set(x for x in str(row.get("exdates") or "").split(",") if x) - set(x for x in str(existing.get("exdates") or "").split(",") if x)
            for key in sorted(new_ex):
                for sib in store.group_masters(existing["link_group_id"]):
                    if sib["id"] == existing["id"]:
                        continue
                    ops.delete_event(store, providers, f"{sib['id']}@{key}", scope="this", owner="companion", cascade=False)
                    propagated += 1
    for row in rows:
        if not row.get("recurrence_id"):
            continue
        master_id = masters.get(row["uid"])
        if not master_id:
            master = store.find_by_external(cal["id"], external_id=row.get("master_external_id") or "", uid=row["uid"])
            master_id = master["id"] if master else ""
        if not master_id:
            continue
        exc = next((e for e in store.exceptions_for(master_id) if e.get("recurrence_id") == row["recurrence_id"]), None)
        payload = {k: v for k, v in row.items() if k != "master_external_id"}
        payload.update({"master_id": master_id, "raw_payload": ""})
        changed = exc is None or exc.get("etag") != row["etag"]
        materially = exc is None or any(str(exc.get(k) or "") != str(row.get(k) or "") for k in ("start_utc", "end_utc", "status"))
        if exc is None:
            store.insert_event(payload)
        elif changed:
            if exc.get("sync_state") in ("pending", "conflict", "pending_delete"):
                continue   # our own write to this occurrence is in flight
            fields = {k: payload[k] for k in ("title", "description", "location", "start_utc", "end_utc", "status", "etag",
                                              "attendees_json", "my_response") if k in payload}
            if exc.get("origin") == "external":
                fields["reminders_json"] = _imported_reminders(exc, payload.get("reminders_json") or "[]", cal["provider"], bool((store.get_setting(rem.MODE_KEY) or {}).get(cal["id"])))
            store.update_event(exc["id"], fields)
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
                        ops.delete_event(store, providers, f"{sib['id']}@{key}", scope="this", owner="companion", cascade=False)
                    else:
                        moved = {"start_utc": row["start_utc"], "end_utc": row["end_utc"], "all_day": row.get("all_day")}
                        ops.update_event(store, providers, f"{sib['id']}@{key}", moved, scope="this", owner="companion", propagate=False)
                    propagated += 1
    if kind == "google_sync_token":
        # Google: cancelled ids in the (incremental) feed are the confirmed deletions (roast Fable F7).
        for ext_id in extra:
            existing = store.find_by_external(cal["id"], external_id=ext_id)
            if existing is not None and not existing.get("deleted_at") and (existing.get("sync_state") not in ("pending", "conflict") or store.delete_requested(existing["id"])):
                deleted += _confirmed_deletion(store, providers, existing)
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
            deleted += _confirmed_deletion(store, providers, local)
    return upserts, deleted, propagated, next_cursor, kind


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


def _confirmed_deletion(store: Store, providers: Providers, local: Dict[str, Any]) -> int:
    if local.get("sync_state") == "pending_delete" or store.delete_requested(local["id"]):
        # This row was selected for deletion by an earlier scoped operation.
        # Its siblings already have their own intents if the owner requested a cascade.
        return int(store.confirm_pending_delete(local["id"]))
    if local.get("link_group_id"):
        ops.delete_event(store, providers, local["id"], scope="all", owner="companion")
    else:
        store.delete_event(local["id"])
    return 1


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
                    health["sync"].append(sync_account(store, providers, account, tz))
                    last_sync[account["id"]] = time.time()
            health["intents"] = ops.retry_due_intents(store, providers, owner="companion")
            planned = rem.plan(store, lambda s, e: ops.expand(store.window(s, e, include_hidden=True), s, e,
                                                                store.exceptions_for, owner_tz=tz, strict=True))
            health["reminders"] = {"planned": planned, **stats}
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
