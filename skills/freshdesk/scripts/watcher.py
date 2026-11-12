#!/usr/bin/env python3
"""Host-supervised companion: polls Freshdesk while the helpdesk-wide place is connected, reports events, delivery
receipts and late outputs. It runs no model and schedules no work: attention is the host's, words are the mind's."""
from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freshdesk_lib.client import FreshdeskError  # noqa: E402
from freshdesk_lib.host import HostLink, HostUnavailable  # noqa: E402
from freshdesk_lib.presence import Presence  # noqa: E402
from freshdesk_lib.sends import Sends  # noqa: E402
from freshdesk_lib.service import KEY_SETTING, Service, read_settings  # noqa: E402
from freshdesk_lib.store import Store, now_iso  # noqa: E402
from freshdesk_lib.watcher import Budget, baseline, process, walk  # noqa: E402

STALE_SENDING_SECONDS = 600  # a tool call still "sending" after this long ended with its process: outcome unknown


@contextlib.contextmanager
def single_instance(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit("another Freshdesk watcher holds this state directory")
    try:
        yield
    finally:
        handle.close()


def recover(store: Store) -> None:
    """Calls a process left mid-way long ago: their effect is unknown, never resent."""
    from datetime import datetime, timezone

    cutoff = time.time() - STALE_SENDING_SECONDS
    for row in store.interrupted_sends():
        try:
            started = datetime.strptime(row["attempted_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            started = 0
        if started < cutoff:
            store.finish(row["request_id"], "uncertain", result={"code": "process_ended_mid_call"},
                         report=row["tool"] != "freshdesk_request")


def note(store: Store, state: str, **extra) -> None:
    store.set_meta("watcher", {"state": state, "at": now_iso(), **extra})


def run(state_dir: str, stop: threading.Event) -> None:
    token = os.environ.get("HOST_SERVICE_TOKEN", "")
    host = HostLink(lambda: token, os.environ.get("HOST_SERVICE_URL", ""))
    service = Service(state_dir, lambda: os.environ.get(KEY_SETTING, ""), host=host)
    sends = Sends(service)
    store = Store(state_dir)
    presence = None
    capabilities, checked, next_walk = {}, 0.0, 0.0
    try:
        recover(store)
        while not stop.is_set():
            try:
                settings = read_settings(state_dir)
                if not settings["domain"] or not os.environ.get(KEY_SETTING, "").strip():
                    note(store, "not_configured", text="Not set up", title="Save the address and the API key.")
                    stop.wait(30)
                    continue
                client = service.client()
                if presence is None or presence.account != client.host:
                    if presence is not None:
                        presence.close()
                    presence = Presence(store, host, sends=sends, account=client.host)
                if not capabilities.get("ready") or time.time() - checked > 600:
                    capabilities, checked = presence.negotiate(), time.time()
                if not capabilities.get("ready"):
                    note(store, "host_capability_gap", text="Host too old",
                         title="This Ouroboros lacks: " + ", ".join(capabilities.get("missing") or []))
                    stop.wait(60)
                    continue
                presence.report_receipts()
                presence.poll_refs()
                presence.deliver_outputs()
                presence.dispatch()
                place = presence.place_state()
                if place == "unbound":
                    note(store, "no_place", text="Not listening",
                         title="Connect the Freshdesk place to hear new tickets and replies.")
                    stop.wait(5)
                    continue
                backoff = store.meta("backoff_until") or 0
                if time.time() >= next_walk and time.time() >= backoff:
                    budget = Budget(store, share=settings["budget_share"], poll_seconds=settings["poll_seconds"])
                    baseline(client, store)
                    result = walk(client, store, budget)
                    process(service, client, store, budget, capabilities=capabilities)
                    next_walk = time.time() + settings["poll_seconds"]
                    backlog = store.one("SELECT COUNT(*) AS n FROM dirty")["n"]
                    waiting = store.one("SELECT COUNT(*) AS n FROM inbox WHERE state IN ('ready','submitting')")["n"]
                    note(store, "listening", last_walk=store.meta("last_walk"), walk_complete=result["complete"],
                         backlog=backlog, events_waiting=waiting, credits_spent=budget.spent, place=place)
                    store.resolve("freshdesk")
            except FreshdeskError as exc:
                if exc.code == "rate_limited":
                    store.set_meta("backoff_until", time.time() + float(exc.retry_after or 60))
                store.gap("freshdesk", exc.code, {"status": exc.status, "message": exc.message[:300]})
                stop.wait(60 if exc.status in (401, 403) else 15)
            except HostUnavailable as exc:
                store.gap("host", exc.code, {"status": exc.status})
                stop.wait(10)
            except (ValueError, OSError) as exc:
                store.gap("watcher", type(exc).__name__, {"message": str(exc)[:300]})
                stop.wait(15)
            stop.wait(2)
    finally:
        if presence is not None:
            presence.close()
        store.close()


def main() -> None:
    state_dir = os.environ.get("OUROBOROS_SKILL_STATE_DIR")
    if not state_dir:
        raise SystemExit("OUROBOROS_SKILL_STATE_DIR is required")
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    with single_instance(Path(state_dir) / "watcher.lock"):
        run(state_dir, stop)


if __name__ == "__main__":
    main()
