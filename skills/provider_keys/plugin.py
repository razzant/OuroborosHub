"""Demand-driven metadata, with bounded refresh wait and no on-disk cache."""
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import keys_adapters as adapters
from .keys_settings import inventory
from .keys_snapshot import assemble

INTERVAL, MANUAL, DEADLINE, PARALLEL = 900, 30, 10, 3


class Board:
    def __init__(self, api):
        self.api = api
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="provider-metadata")
        self.creds, self.absent, self.settings = [], [], {}
        self.ids, self.records, self.pending, self.order = {}, {}, {}, {}
        self.serial = 0
        self.closed = self.running = False
        self.started = self.finished = None
        self.finished_mono = float("-inf")
        self.partial, self.dirty = False, True

    def sync(self):
        try:
            creds, absent, settings, keys = inventory(self.api)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError, RecursionError):
            creds, absent, settings, keys = [], [], {
                "state": "settings_unreadable", "not_granted": [], "disabled": [],
            }, {}
        if (self.creds, self.absent, self.settings) == (creds, absent, settings):
            return keys
        self.ids = {c.identity: self.ids.get(c.identity) or uuid.uuid4().hex for c in creds}
        valid = set(self.ids.values())
        self.records = {k: v for k, v in self.records.items() if k[0] in valid}
        self.order = {k: v for k, v in self.order.items() if k[0] in valid}
        self.creds, self.absent, self.settings = creds, absent, settings
        self.dirty = True
        return keys

    def view(self, skipped=None):
        refreshing = self.running and self.settings.get("state") != "access_closed"
        refresh = dict(state="refreshing" if refreshing else "idle", last_started_at=self.started,
                       last_finished_at=self.finished, interval_s=INTERVAL, min_manual_s=MANUAL)
        return assemble(self.creds, self.absent, self.settings, self.ids, self.records,
                        time.time(), refresh, skipped or {})

    def snapshot(self):
        with self.lock:
            if not self.closed:
                self.sync()
            return self.view()

    def fetch(self, cred, ident, deadline):
        with self.lock:
            if self.closed:
                return None
            key = self.sync().get(cred.identity)
            if not key or self.ids.get(cred.identity) != ident[0]:
                return None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            return adapters.read(cred.kind, key, ident[1], time.time(), min(8, remaining))
        return None

    def respond(self, reason, counts=None, skipped=None):
        outcome = {"reason": reason, **(counts or dict(succeeded=0, failed=0, deferred=0, retry_wait=0, unfinished=0))}
        return {"accepted": reason in ("finished", "partial"), "outcome": outcome, "snapshot": self.view(skipped)}

    def refresh(self, manual=True):
        with self.lock:
            if self.closed:
                return self.respond("access_closed")
            self.sync()
            if self.settings["state"] in ("settings_unreadable", "access_closed"):
                return self.respond(self.settings["state"])
            if self.running:
                return self.respond("already_refreshing")
            jobs, skipped = [], {}
            now = time.time()
            self.reap({}, {})
            pending_ids = set(self.pending.values())
            for cred in self.creds:
                if cred.reason:
                    continue
                for ep in adapters.URLS.get(cred.kind, {}):
                    ident = self.ids[cred.identity], ep
                    old = self.records.get(ident, ({}, {}, None))[0]
                    retry = adapters.seconds(old.get("retry_at"))
                    if retry is not None and retry > now:
                        skipped[ident] = "retry_wait"
                    elif ident in pending_ids:
                        skipped[ident] = "unfinished"
                    else:
                        jobs.append((ident, cred, self.order.get(ident, 0)))
            if not jobs:
                reason = "waiting" if skipped else "no_readable_keys"
                return self.respond(reason, self.counts(skipped), skipped)
            due = MANUAL if manual or self.partial else INTERVAL
            if not self.dirty and time.monotonic() - self.finished_mono < due:
                return self.respond("too_soon", self.counts(skipped), skipped)
            # Never-attempted first, then least recently attempted. Keep only up to
            # PARALLEL physical requests in flight even if an OS call outlives the wait.
            jobs.sort(key=lambda job: job[2])
            self.running, self.dirty = True, False
            self.started = adapters.timestamp(now)
        return self.collect(jobs, skipped)

    @staticmethod
    def counts(skipped):
        return {"succeeded": 0, "failed": 0, **{k: sum(v == k for v in skipped.values())
                for k in ("deferred", "retry_wait", "unfinished")}}

    def collect(self, jobs, skipped):
        deadline = time.monotonic() + DEADLINE
        queue = iter(jobs)
        active, results = {}, {}
        try:
            while time.monotonic() < deadline:
                with self.lock:
                    if self.closed:
                        break
                    self.reap(active, results)
                    if len(results) == len(jobs):
                        break
                    for _ in range(PARALLEL - len(self.pending)):
                        job = next(queue, None)
                        if job is None:
                            break
                        ident, cred, _ = job
                        f = self.pool.submit(self.fetch, cred, ident, deadline)
                        self.pending[f] = active[f] = ident
                        self.serial += 1
                        self.order[ident] = self.serial
                    physical = tuple(self.pending)
                if not physical:
                    break
                # Wait off the host event loop. Even a socket stalled past its budget
                # holds its own slot until it actually returns; never queue more behind it.
                wait(physical, timeout=max(0, deadline-time.monotonic()), return_when=FIRST_COMPLETED)
        finally:
            with self.lock:
                self.reap(active, results)
                for f, ident in active.items():
                    skipped[ident] = "deferred" if f.cancel() else "unfinished"
                for ident, _, _ in jobs:
                    if ident not in results and ident not in skipped:
                        skipped[ident] = "deferred"
                self.running = False
                if not self.closed:
                    self.sync()  # Recheck latest grants and credentials before publishing facts.
                valid = set(self.ids.values())
                skipped = {i: v for i, v in skipped.items() if i[0] in valid}
                counts = self.counts(skipped)
                for ident, result in results.items():
                    if ident[0] not in valid:
                        continue
                    counts["failed" if result[0]["reason"] else "succeeded"] += 1
                    self.remember(ident, result)
                self.partial = bool(counts["unfinished"] or counts["deferred"])
                # Keep skipped states in cached GET/HEAD responses until a later attempt.
                for ident, phase in skipped.items():
                    meta, fields, account = self.records.get(ident, ({}, {}, None))
                    self.records[ident] = {**meta, "phase": phase}, fields, account
                if jobs:
                    self.finished_mono = time.monotonic()
                if counts["succeeded"] or counts["failed"]:
                    self.finished = adapters.timestamp(time.time())
                if self.closed:
                    reason = "access_closed"
                elif self.settings["state"] in ("settings_unreadable", "access_closed"):
                    reason = self.settings["state"]
                elif sum(counts.values()):
                    reason = "partial" if self.partial else "finished"
                else:
                    reason = "settings_changed"
                response = self.respond(reason, counts, skipped)
        return response

    def remember(self, ident, result):
        meta, fields, account = result
        if meta["reason"] and meta["reason"] not in ("key_rejected", "api_key_invalid"):
            previous, fields, account = self.records.get(ident, ({}, {}, None))
            meta = {**meta, "ok_at": previous.get("ok_at"), "read_started_at": previous.get("read_started_at")}
        self.records[ident] = meta, fields, account

    def reap(self, active, results):
        for f, ident in tuple(self.pending.items()):
            if not f.done():
                continue
            del self.pending[f]
            current = active.pop(f, None) is not None
            if f.cancelled() or (result := f.result()) is None:
                continue
            if current:
                results[ident] = result
            elif result[0]["status"] == 429 and ident[0] in self.ids.values():
                # A previous refresh dropped this result at its wait deadline. Keep only
                # the provider's still-active retry fence for that exact surviving binding.
                retry = adapters.seconds(result[0].get("retry_at"))
                if retry is not None and retry > time.time():
                    self.remember(ident, result)

    def close(self):
        with self.lock:
            self.closed = True
            self.creds, self.absent, self.ids, self.records, self.order = [], [], {}, {}, {}
            self.settings = {"state": "access_closed", "not_granted": [], "disabled": []}
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pending.clear()


def register(api):
    board = Board(api)
    # The host dispatches synchronous extension handlers in its threadpool.
    # A compatible host handles HEAD automatically for GET routes.
    api.register_route("snapshot", lambda request: board.snapshot(), methods=("GET",))
    api.register_route("refresh", lambda request: board.refresh(manual=getattr(request, "query_params", {}).get("mode") != "auto"),
                       methods=("POST",))
    api.register_ui_tab("keys", "Provider keys", icon="⚿",
                        render=dict(kind="module", entry="widget.js", appearance="host", start="auto", span=2))
    api.on_unload(board.close)
