"""One kept custody connection: idle polls open no file and take no write lock; FULL durability.

The runtime's five pollers call the store about ten times a second while nothing waits. Each call
opened a new connection, and each claim began BEGIN IMMEDIATE before it knew there was work, which
recreated the WAL file on every poll. These guards keep an idle poll a plain read on a connection
that stays open, while a claim with work still leases exactly what it leased before.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from telegram_bot import custody
from telegram_bot.custody import CustodyStore
from telegram_bot.events import parse_telegram_update
from telegram_bot.runtime import TelegramTransportRuntime
from test_group_discovery import _plugin
from test_plugin import FakeApi, FakeRequest
from test_runtime import Logger, RuntimeClient


def event(update_id, chat=4):
    return parse_telegram_update({"update_id": update_id, "message": {
        "message_id": 100 + update_id, "text": f"hello {update_id}", "from": {"id": 3},
        "chat": {"id": chat, "type": "private"}}}, bot_account_id="9")


def deferred_work(store, update_id, chat):
    """One observed Host reference, through the store's own submission path."""
    store.commit_update(update_id, event(update_id, chat))
    lease = store.claim_inbox()
    store.record_submission(lease.event_id, binding_id="b" * 32, turn_ref=f"turn-{update_id}",
                            outcome="deferred", text="", work_ref=f"work-{update_id}")


def read(path, sql, args=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def wal(path):
    return Path(f"{path}-wal")


def idle_poll(store):
    """Everything the runtime's idle workers ask the store each pass."""
    assert store.claim_inbox(lease_sec=1860.0) is None
    assert store.claim_work(lease_sec=120.0) is None
    assert store.claim_outbox(lease_sec=120.0) is None
    assert store.next_delivery_report() is None
    store.telegram_offset()


@pytest.mark.parametrize("populated", [False, True])
def test_an_idle_poll_opens_no_file_takes_no_write_lock_and_writes_nothing(tmp_path, monkeypatch, populated):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    # Rows that are not claimable now: delivered, leased by a live worker, or due later.
    if populated:
        store.enqueue_outbox("done", {"kind": "message", "chat_id": "4", "text": "sent"})
        store.mark_delivered(store.claim_outbox().delivery_id, provider_receipt={"message_id": 1})
        store.commit_update(1, event(1))
        assert store.claim_inbox(lease_sec=600) is not None
        deferred_work(store, 2, chat=5)
        store.release_work(store.claim_work().work_ref, reason="presence_work_pending", retry_after_sec=600)
    idle_poll(store)
    before = os.stat(wal(store.path))
    statements, opened = [], []
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: opened.append(args) or connect(*args, **kwargs))
    with store._connect() as conn:
        conn.set_trace_callback(statements.append)
    for _ in range(25):
        idle_poll(store)
    after = os.stat(wal(store.path))
    assert opened == []
    assert statements and not [sql for sql in statements if not sql.lstrip().upper().startswith("SELECT")]
    # The same WAL file, never deleted and recreated, and not one frame longer.
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)


def test_a_claimable_row_is_decided_again_under_the_write_lock(tmp_path):
    """The plain read only says "there may be work"; BEGIN IMMEDIATE and the CAS decide, as before."""
    path = tmp_path / "custody.sqlite3"
    store, rival = CustodyStore(path), CustodyStore(path)
    store.commit_update(1, event(1, chat=4))
    store.commit_update(2, event(2, chat=5))
    taken = []

    def rival_claims_first(sql):
        # Runs as our BEGIN IMMEDIATE starts, after our plain read saw update 1 free.
        if sql.strip().upper() == "BEGIN IMMEDIATE" and not taken:
            taken.append(rival.claim_inbox(lease_sec=600).event_id)

    with store._connect() as conn:
        conn.set_trace_callback(rival_claims_first)
    lease = store.claim_inbox(lease_sec=600)
    with store._connect() as conn:
        conn.set_trace_callback(None)
    assert taken == ["telegram:9:1"]
    assert lease.event_id == "telegram:9:2" and lease.attempts == 1
    assert store.claim_inbox() is None and rival.claim_inbox() is None
    assert read(path, "SELECT event_id, state, attempts FROM inbox ORDER BY update_id") == [
        ("telegram:9:1", "leased", 1), ("telegram:9:2", "leased", 1)]


@pytest.mark.parametrize("queue", ["inbox", "work", "outbox"])
@pytest.mark.parametrize("shared_connection", [False, True])
def test_concurrent_claimers_never_lease_the_same_row(tmp_path, queue, shared_connection):
    path = tmp_path / "custody.sqlite3"
    seed = CustodyStore(path)
    rows = 24
    for index in range(rows):
        if queue == "inbox":
            seed.commit_update(index + 1, event(index + 1, chat=1000 + index))
        elif queue == "work":
            deferred_work(seed, index + 1, chat=1000 + index)
        else:
            seed.enqueue_outbox(f"d{index}", {"kind": "message", "chat_id": str(1000 + index), "text": "x"})
    claim = {"inbox": lambda s: s.claim_inbox(lease_sec=600), "work": lambda s: s.claim_work(lease_sec=600),
             "outbox": lambda s: s.claim_outbox(lease_sec=600)}[queue]
    identity = {"inbox": "event_id", "work": "work_ref", "outbox": "delivery_id"}[queue]
    stores = [seed] * 3 if shared_connection else [CustodyStore(path) for _ in range(3)]
    start, claimed = threading.Barrier(len(stores)), [[] for _ in stores]

    def claimer(index):
        start.wait()
        while (lease := claim(stores[index])) is not None:
            claimed[index].append(getattr(lease, identity))

    threads = [threading.Thread(target=claimer, args=(index,)) for index in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert all(not thread.is_alive() for thread in threads)
    every = [item for items in claimed for item in items]
    assert len(every) == len(set(every)) == rows
    assert all(claim(store) is None for store in stores)


def test_the_kept_connection_commits_with_full_durability_after_initialize(tmp_path):
    """_initialize once asked for NORMAL on its own connection; the kept one must stay FULL."""
    for store in (CustodyStore(tmp_path / "custody.sqlite3"), CustodyStore.shared(tmp_path / "other.sqlite3")):
        with store._connect() as conn:
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        store.commit_update(1, event(1))
        with store._connect() as conn:
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_an_error_inside_a_transaction_leaves_no_write_lock_behind(tmp_path):
    path = tmp_path / "custody.sqlite3"
    store = CustodyStore(path)
    with pytest.raises(ValueError):
        store.record_work_outputs("missing", [])  # raises after BEGIN IMMEDIATE
    with store._connect() as conn:
        assert not conn.in_transaction
    other = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()
    assert store.enqueue_outbox("after", {"kind": "message", "chat_id": "4", "text": "x"})


def test_one_process_shares_one_store_and_initializes_its_file_once(tmp_path, monkeypatch):
    module = _plugin()  # a process of its own, with its own custody module
    calls, initialize = [], module.CustodyStore._initialize
    monkeypatch.setattr(module.CustodyStore, "_initialize", lambda self: calls.append(self.path) or initialize(self))
    api = FakeApi(tmp_path)
    module.register(api)
    tools = {name: handler for name, handler, _meta in api.tools}
    assert tools["telegram_send"](chat_id="4", text="one", request_id="r1")["state"] == "queued"
    assert tools["telegram_moderate"](action="delete_message", chat_id="-4", message_id=3,
                                      request_id="m1")["state"] == "queued"
    assert tools["telegram_receipt"](request_id="r1")["state"] == "pending"
    assert module.CustodyStore.shared(tmp_path / "custody.sqlite3") is module._RUNTIME.store
    assert calls == [tmp_path / "custody.sqlite3"]
    api.unload()


def test_unload_closes_the_connection_and_a_new_registration_opens_it_again(tmp_path):
    module = _plugin()
    api = FakeApi(tmp_path)
    module.register(api)
    store = module._RUNTIME.store
    store.enqueue_outbox("before", {"kind": "message", "chat_id": "4", "text": "x"})
    assert store._conn is not None and wal(store.path).exists()
    api.unload()
    # The only connection closed: SQLite checkpointed and removed the WAL file.
    assert store._conn is None and not wal(store.path).exists()
    again = FakeApi(tmp_path)
    module.register(again)
    restarted = module._RUNTIME.store
    lease = restarted.claim_outbox()
    assert lease.delivery_id == "before" and lease.attempts == 1
    again.unload()
    assert restarted._conn is None


def test_a_stopped_run_releases_its_lease_and_then_closes_the_store(tmp_path):
    class Blocked:
        """A Host turn that never answers, so the worker holds its lease when stopped."""
        started = None

        async def submit(self, event, staged_files):
            self.started.set()
            await asyncio.Event().wait()

        async def poll(self, work_ref, binding_id):
            raise AssertionError("no work")

    class QuietTelegram(RuntimeClient):
        async def get_updates(self, *, offset, timeout_sec):
            await asyncio.sleep(0.05)
            return []

    async def run():
        submitter = Blocked()
        submitter.started = asyncio.Event()
        runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                           submitter=submitter, client_factory=QuietTelegram)
        runtime.store.commit_update(1, event(1))
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(submitter.started.wait(), 10)
        assert runtime.store._conn is not None
        runtime.stop()
        await asyncio.gather(task, return_exceptions=True)
        return runtime

    runtime = asyncio.run(run())
    assert runtime.store._conn is None and not wal(runtime.store.path).exists()
    assert read(runtime.store.path, "SELECT state, last_error FROM inbox") == [("pending", "worker_cancelled")]
    # The next run, or a status read, opens it again.
    assert runtime.store.status_snapshot()["inbox_waiting"] == 1


def test_a_run_that_fails_before_starting_workers_closes_its_store(tmp_path, monkeypatch):
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=Logger(),
                                       submitter=None)

    def failed_recovery():
        raise sqlite3.OperationalError("recovery failed")

    monkeypatch.setattr(runtime.store, "release_own_leases", failed_recovery)
    with pytest.raises(sqlite3.OperationalError, match="recovery failed"):
        asyncio.run(runtime.run())
    assert runtime._run_task is None and runtime.store._conn is None


def test_status_and_settings_routes_do_their_reads_and_writes_beside_the_http_loop(tmp_path, monkeypatch):
    """A blocked file read must not stop the loop that serves the Host's HTTP requests."""
    module = _plugin()
    api = FakeApi(tmp_path)
    module.register(api)
    loop_free, threads = threading.Event(), []

    def blocking(real):
        def call(*args, **kwargs):
            threads.append(threading.get_ident())
            assert loop_free.wait(5), "the HTTP loop was blocked while the route read its files"
            return real(*args, **kwargs)
        return call

    monkeypatch.setattr(module, "_status_payload", blocking(module._status_payload))
    monkeypatch.setattr(module, "_load_local_settings", blocking(module._load_local_settings))
    monkeypatch.setattr(module, "_save_local_settings", blocking(module._save_local_settings))

    async def served(call):
        loop_free.clear()
        route = asyncio.ensure_future(call)
        await asyncio.sleep(0.05)  # the loop keeps turning while the route waits on its read
        loop_free.set()
        return await route

    async def run():
        status = json.loads((await served(module._status_route(None))).body)
        saved = await served(module._settings_save_route(FakeRequest({"management_group_id": "-42"})))
        current = json.loads((await served(module._settings_save_route(FakeRequest(method="GET")))).body)
        return threading.get_ident(), status, saved, current

    loop_thread, status, saved, current = asyncio.run(run())
    assert status["title"] == "Telegram transport" and status["inbox_waiting"] == 0
    assert saved.status_code == 200 and current["management_group_id"] == "-42"
    assert json.loads((tmp_path / "settings.json").read_text())["management_group_id"] == "-42"
    assert threads and loop_thread not in threads
    api.unload()


def test_two_settings_saves_at_once_both_keep_their_fields(tmp_path, monkeypatch):
    module = _plugin()
    api = FakeApi(tmp_path)
    module.register(api)
    load = module._load_local_settings

    def slow_load():
        value = load()
        time.sleep(0.1)  # long enough for the other save to read the same file meanwhile
        return value

    monkeypatch.setattr(module, "_load_local_settings", slow_load)

    async def run():
        return await asyncio.gather(module._settings_save_route(FakeRequest({"management_group_id": "-42"})),
                                    module._settings_save_route(FakeRequest({"words": "invoice"})))

    assert [response.status_code for response in asyncio.run(run())] == [200, 200]
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["management_group_id"] == "-42" and saved["words"] == ["invoice"]
    api.unload()


def test_a_shared_store_is_released_when_nothing_holds_it(tmp_path):
    path = tmp_path / "custody.sqlite3"
    first = CustodyStore.shared(path)
    assert CustodyStore.shared(path) is first
    with first._connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    key = os.path.realpath(path)
    del first
    assert key not in custody._SHARED
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
