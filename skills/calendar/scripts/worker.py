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
from model import PROVIDER_YANDEX, get_tz, iso_utc, now_utc, parse_stored  # noqa: E402
from providers import Providers  # noqa: E402
from store import Store  # noqa: E402

TICK_SEC = 60
SYNC_INTERVAL_SEC = 180
SECRET_KEYS = ("YANDEX_CALDAV_ACCOUNTS", "GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET", "CALENDAR_TOKEN_KEY")
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
        store.set_account_status(account["id"], "not_connected", "нет секрета или гранта для этого аккаунта")
        return {"account": account["id"], "status": "not_connected"}
    report: Dict[str, Any] = {"account": account["id"], "calendars": 0, "events": 0, "deleted": 0, "propagated": 0}
    try:
        caps = store.get_setting(f"capabilities:{account['id']}") or {}
        if not caps:
            caps = adapter.probe()
            store.set_setting(f"capabilities:{account['id']}", caps)
        cals = adapter.list_calendars()
        for cal in cals:
            existing = store.get_calendar(cal["id"])
            store.upsert_calendar({**cal, "role_visible": existing["role_visible"] if existing else True,
                                   "role_busy": existing["role_busy"] if existing else True,
                                   "role_publish": existing["role_publish"] if existing else False,
                                   "publish_mode": existing["publish_mode"] if existing else "busy"})
            report["calendars"] += 1
            stored = store.get_calendar(cal["id"])
            ctag = cal.get("ctag") or ""
            state = store.get_sync_state(cal["id"])
            if ctag and state.get("cursor") == ctag and state.get("cursor_kind") == "ctag" and state.get("last_ok_at"):
                continue  # nothing changed on the server since the last pass
            n, d, p = reconcile_calendar(store, providers, adapter, stored, tz)
            report["events"] += n
            report["deleted"] += d
            report["propagated"] += p
            store.set_sync_state(cal["id"], cursor=ctag, cursor_kind="ctag" if ctag else "window", last_ok_at=iso_utc(now_utc()), last_error="")
        store.set_account_status(account["id"], "ok", "", synced=True)
    except ops.ProviderError as exc:
        status = "auth_failed" if exc.kind == "auth" else "error"
        store.set_account_status(account["id"], status, exc.message)
        report["status"] = status
        report["error"] = exc.message
        return report
    report["status"] = "ok"
    return report


def reconcile_calendar(store: Store, providers: Providers, adapter, cal: Dict[str, Any], tz):
    """Server window → local rows. Our own linked copies get external edits propagated; confirmed deletions cascade (28 A)."""
    rows, _cursor, _kind, hrefs = adapter.fetch(cal, tz=tz)
    seen_hrefs = set(hrefs)
    upserts = deleted = propagated = 0
    masters: Dict[str, str] = {}
    for row in rows:
        if row.get("recurrence_id"):
            continue
        existing = store.find_by_external(cal["id"], external_id=row["external_id"], href=row["href"], uid=row["uid"])
        if existing is None:
            saved = store.insert_event(row)
            masters[row["uid"]] = saved["id"]
            upserts += 1
            continue
        masters[row["uid"]] = existing["id"]
        if existing.get("etag") == row["etag"] and existing.get("sync_state") == "synced":
            continue
        if existing.get("sync_state") in ("pending", "conflict"):
            continue  # our write is in flight; the intent path settles it
        changes = {k: row[k] for k in ("title", "description", "location", "start_utc", "end_utc", "tz", "all_day", "rrule", "exdates", "rdates",
                                        "status", "organizer", "attendees_json", "etag", "raw_payload")}
        if existing.get("origin") == "external":
            changes["reminders_json"] = row["reminders_json"]
        store.update_event(existing["id"], {**changes, "sync_state": "synced"})
        upserts += 1
        if existing.get("link_group_id") and (existing.get("start_utc") != row["start_utc"] or existing.get("end_utc") != row["end_utc"]):
            moved = {"start_utc": row["start_utc"], "end_utc": row["end_utc"], "all_day": row["all_day"]}
            for sib in store.group_members(existing["link_group_id"]):
                if sib["id"] == existing["id"]:
                    continue
                ops.update_event(store, providers, sib["id"], moved, scope="all", owner="companion", propagate=False)
                propagated += 1
    for row in rows:
        if not row.get("recurrence_id"):
            continue
        master_id = masters.get(row["uid"])
        if not master_id:
            continue
        exc = next((e for e in store.exceptions_for(master_id) if e.get("recurrence_id") == row["recurrence_id"]), None)
        payload = {**row, "master_id": master_id, "raw_payload": ""}
        if exc is None:
            store.insert_event(payload)
        elif exc.get("etag") != row["etag"]:
            store.update_event(exc["id"], {k: payload[k] for k in ("title", "description", "location", "start_utc", "end_utc", "status", "etag")})
        upserts += 1
    # Deletions: external rows inside the fetched window whose resource is gone.
    window_start = now_utc() - timedelta(days=30)
    for local in store.window(window_start, now_utc() + timedelta(days=400), [cal["id"]], include_hidden=True, include_masters=True):
        if local.get("master_id") or not local.get("href") or local["href"] in seen_hrefs:
            continue
        if local.get("sync_state") in ("pending", "conflict"):
            continue
        try:
            still_there = adapter.exists(local["href"])
        except ops.ProviderError:
            continue  # access problem is not a deletion
        if still_there:
            continue
        if local.get("link_group_id"):
            ops.delete_event(store, providers, local["id"], scope="all", owner="companion")
        else:
            store.delete_event(local["id"])
        deleted += 1
    return upserts, deleted, propagated


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
    _log("companion started", state_dir=state_dir, deps=deps, host_service=bool(os.environ.get("HOST_SERVICE_URL")))
    last_sync: Dict[str, float] = {}
    tick = 0
    while not _stop:
        tick += 1
        tz = get_tz(store.get_setting("timezone") or "")
        health: Dict[str, Any] = {"deps": deps, "tick": tick, "sync": [], "intents": None, "reminders": None, "channel": None}
        try:
            requested = parse_stored(store.get_setting("sync_requested_at") or "")
            for account in store.list_accounts(provider=PROVIDER_YANDEX):
                due = time.time() - last_sync.get(account["id"], 0) >= SYNC_INTERVAL_SEC
                forced = requested is not None and (requested.timestamp() > last_sync.get(account["id"], 0))
                if due or forced:
                    health["sync"].append(sync_account(store, providers, account, tz))
                    last_sync[account["id"]] = time.time()
            health["intents"] = ops.retry_due_intents(store, providers, owner="companion")
            planned = rem.plan(store, lambda s, e: ops.expand(store.window(s, e, include_hidden=True), s, e, store.exceptions_for))
            stats = rem.deliver_due(store, channel, tz)
            health["reminders"] = {"planned": planned, **stats}
            state = channel.state(refresh=(tick % 10 == 1))
            health["channel"] = state
            store.set_setting("notify_channel_state", {"state": state, "checked_at": iso_utc(now_utc())})
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
