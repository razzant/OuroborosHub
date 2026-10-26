#!/usr/bin/env python3
"""Host-supervised network companion; no model or independent followup scheduler."""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gh_presence.collector import Collector
from gh_presence.config import SECRET_KEYS, connection_account, provider_http, read_config, writer_http
from gh_presence.host import Host
from gh_presence.http import HTTP, TransportError
from gh_presence.locking import CompanionAlreadyRunning, CompanionLock
from gh_presence.outbox import Outbox, Sender
from gh_presence.provider import GitHub
from gh_presence.store import Store, dump, now


def build(store, config, environment):
    namespace = connection_account(config)
    prior = store.meta("source_namespace")
    historical = store.one("SELECT event FROM inbox LIMIT 1")
    if (prior and prior != namespace) or (historical and json.loads(historical["event"])["account_id"] != namespace):
        raise TransportError("state_namespace_changed_preserve_old_custody")
    store.set_meta("source_namespace", namespace)
    writer = GitHub(writer_http(config, environment), store, config=config)
    writer.identity(config["expected_writer_id"])
    for selected in config["watched_identities"]:
        writer.watched_identity(selected)
    readers = {}
    if config["writer_notifications"]:
        reader = GitHub(provider_http(config, environment.get(SECRET_KEYS[0], ""), readonly=True), store, "writer", config=config)
        reader.network_lock = writer.network_lock
        readers[config["expected_writer_id"]] = reader
    if config["notifications_user_id"]:
        try:
            reader = GitHub(provider_http(config, environment.get(SECRET_KEYS[1], ""), readonly=True), store, "notifications", config=config)
            reader.network_lock = writer.network_lock
            # The collector verifies this reader before any feed/discovery
            # query. Transient failures retain the job and its normal backoff.
            readers[config["notifications_user_id"]] = reader
        except (TransportError, ValueError) as exc:
            store.gap("notifications_identity", getattr(exc, "code", "notifications_credential_missing"),
                      {"retry": "controlled companion restart after credential repair"})
    host = Host(HTTP(environment.get("HOST_SERVICE_URL", ""), environment.get("HOST_SERVICE_TOKEN", ""),
                     host=True, timeout=45), store, Outbox(store, config))
    try:
        host.negotiate()
    except BaseException:
        host.close()
        raise
    return host, Collector(store, writer, config, readers,
                           unverified_notifications=[config["notifications_user_id"]]), Sender(store, writer, host, config)


def main():
    state = os.environ.get("OUROBOROS_SKILL_STATE_DIR")
    if not state:
        raise SystemExit("OUROBOROS_SKILL_STATE_DIR is required")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    Path(state).mkdir(parents=True, exist_ok=True)
    try:
        with CompanionLock(Path(state) / "companion.lock"):
            run(state, stop)
    except CompanionAlreadyRunning as exc:
        raise SystemExit(str(exc)) from None


def run(state, stop):
    # Recovery and every worker stay inside the lock's lifetime, including
    # orderly shutdown while a physical request is still in flight.
    store = Store(state)
    store.recover()
    host = None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="github-transport")
    background = {}
    old_config = None
    try:
        while not stop.is_set():
            try:
                config = read_config(state)
                if config != old_config:
                    if background:
                        # Finish existing physical calls before rebinding credentials.
                        if any(not f.done() for f in background.values()):
                            stop.wait(1)
                            continue
                        for f in background.values():
                            f.result()
                        background = {}
                    if host:
                        host.close()
                        host = None
                    old_config = config
                if not config["enabled"]:
                    store.set_meta("runtime", {"state": "disabled", "at": now()})
                    stop.wait(2)
                    continue
                if host is None:
                    host, collector, sender = build(store, config, os.environ)
                host.dispatch()
                # No whole-thread read, slow work poll or receipt ACK blocks
                # admission of a new arrival. Each lane has one in-flight job.
                def collect():
                    with contextlib.ExitStack() as stack:
                        for provider in [collector.provider, *collector.notifications.values()]:
                            stack.enter_context(provider.collection())
                        collector.seed()
                        collector.tick()
                def service_host():
                    host.poll()
                    host.reports()
                    host.reconsiderations()
                def send():
                    sender.repair_reports()
                    sender.tick()
                    sender.reconcile_uncertain()
                for name, fn in (("collect", collect), ("host", service_host), ("send", send)):
                    if name in background and background[name].done():
                        background.pop(name).result()
                    if name not in background:
                        background[name] = pool.submit(fn)
                store.set_meta("runtime", {"state": "running", "at": now()})
                store.resolve("runtime")
            except (ValueError, OSError, TransportError) as exc:
                # Never print provider exception bodies, tokens or malformed settings.
                store.gap("runtime", getattr(exc, "code", type(exc).__name__))
                stop.wait(15)
            stop.wait(1)
    finally:
        pool.shutdown(wait=True)
        if host:
            host.close()
        store.close()


if __name__ == "__main__":
    main()
