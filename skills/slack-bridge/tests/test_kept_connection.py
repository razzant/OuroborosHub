"""One kept store connection: idle workers open no file and take no write lock; FULL durability.

The companion's workers ask the store about every 0.25 s while nothing waits. Each call opened
a new connection, and each claim began BEGIN IMMEDIATE before it knew there was work. These
guards keep an idle claim a plain read on a connection that stays open, while a claim with work
leases, settles and moves exactly what it did before.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from lib.events import parse_socket_envelope
from lib.store import BridgeStore
from test_workers_plugin import _Api, _Request, _load_plugin


def envelope(index, channel="C1", thread=""):
    event = {"type": "message", "user": "U2", "text": f"hello {index}", "channel": channel,
             "channel_type": "channel", "ts": f"{100 + index}.000100", "event_ts": f"{100 + index}.000100",
             "team": "T1"}
    if thread:
        event["thread_ts"] = thread
    return {"envelope_id": f"env-{index}", "type": "events_api",
            "payload": {"team_id": "T1", "event_id": f"Ev{index}", "event": event}}


def ingest(store, index, channel="C1"):
    raw = envelope(index, channel)
    store.ingest_envelope(raw, parse_socket_envelope(raw))


def read(path, sql, args=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def write(path, sql, args=()):
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(sql, args)
    finally:
        conn.close()


def idle_poll(store):
    """What the companion's idle intake, observe and outbound workers ask each pass."""
    assert store.claim_inbox(lease_seconds=60, submitted=False) is None
    assert store.claim_inbox(lease_seconds=60, submitted=True) is None
    assert store.claim_inbox(lease_seconds=60) is None
    assert store.claim_outbox(lease_seconds=120) is None
    assert store.claim_report() is None


@pytest.mark.parametrize("populated", [False, True])
def test_an_idle_worker_opens_no_file_takes_no_write_lock_and_writes_nothing(tmp_path, monkeypatch, populated):
    store = BridgeStore(tmp_path)
    # Rows that are not claimable now: a delivered send, a live lease and a retry due later.
    if populated:
        store.enqueue_outbox(request_id="sent", target="C1", thread_ts="", chunks=["sent"])
        sent = store.claim_outbox()
        store.complete_outbox(sent.row_id, sent.lease_token, provider_message_ts="1.0")
        ingest(store, 1, channel="C1")
        assert store.claim_inbox(lease_seconds=600) is not None
        ingest(store, 2, channel="C2")
        later = store.claim_inbox(lease_seconds=600)
        store.retry_inbox(later.row_id, later.lease_token, "later", delay_seconds=600)
    idle_poll(store)
    wal = Path(f"{store.path}-wal")
    before = os.stat(wal)
    statements, opened = [], []
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: opened.append(args) or connect(*args, **kwargs))
    with store._connect() as db:
        db.set_trace_callback(statements.append)
    for _ in range(25):
        idle_poll(store)
    after = os.stat(wal)
    assert opened == []
    assert statements and not [sql for sql in statements if not sql.lstrip().upper().startswith("SELECT")]
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)


def test_a_claimable_row_is_decided_again_under_the_write_lock(tmp_path):
    store, rival = BridgeStore(tmp_path), BridgeStore(tmp_path)
    ingest(store, 1, channel="C1")
    ingest(store, 2, channel="C2")
    taken = []

    def rival_claims_first(sql):
        # Runs as our BEGIN IMMEDIATE starts, after our plain read saw the first event free.
        if sql.strip().upper() == "BEGIN IMMEDIATE" and not taken:
            taken.append(rival.claim_inbox(lease_seconds=600).event_id)

    with store._connect() as db:
        db.set_trace_callback(rival_claims_first)
    item = store.claim_inbox(lease_seconds=600)
    with store._connect() as db:
        db.set_trace_callback(None)
    assert taken == ["Ev1"] and item.event_id == "Ev2" and item.attempts == 1
    assert store.claim_inbox() is None and rival.claim_inbox() is None
    assert read(store.path, "SELECT event_id, state, attempts FROM inbox ORDER BY id") == [
        ("Ev1", "leased", 1), ("Ev2", "leased", 1)]


@pytest.mark.parametrize("queue", ["inbox", "outbox", "report"])
@pytest.mark.parametrize("shared_connection", [False, True])
def test_concurrent_claimers_never_lease_the_same_row(tmp_path, queue, shared_connection):
    seed = BridgeStore(tmp_path)
    rows = 24
    for index in range(rows):
        if queue == "inbox":
            ingest(seed, index + 1, channel=f"C{index}")
        else:
            seed.enqueue_outbox(request_id=f"r{index}", target=f"C{index}", thread_ts="", chunks=["x"])
    if queue == "report":
        while (item := seed.claim_outbox()) is not None:
            seed.complete_outbox(item.row_id, item.lease_token, provider_message_ts="1.0",
                                 report_payload={"delivery_id": item.request_id})
    claim = {"inbox": lambda s: s.claim_inbox(lease_seconds=600),
             "outbox": lambda s: s.claim_outbox(lease_seconds=600),
             "report": lambda s: s.claim_report()}[queue]
    identity = {"inbox": lambda item: item.row_id, "outbox": lambda item: item.row_id,
                "report": lambda item: item["row_id"]}[queue]
    stores = [seed] * 3 if shared_connection else [BridgeStore(tmp_path) for _ in range(3)]
    start, claimed = threading.Barrier(len(stores)), [[] for _ in stores]

    def claimer(index):
        start.wait()
        while (item := claim(stores[index])) is not None:
            claimed[index].append(identity(item))

    threads = [threading.Thread(target=claimer, args=(index,)) for index in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert all(not thread.is_alive() for thread in threads)
    every = [item for items in claimed for item in items]
    assert len(every) == len(set(every)) == rows
    assert all(claim(store) is None for store in stores)


def test_a_lost_send_attempt_is_settled_uncertain_even_when_nothing_else_is_claimable(tmp_path):
    """The read before the write lock also looks for the sweep's rows: a row that may have
    reached Slack becomes uncertain and is never sent again, whether or not its lane is free."""
    store = BridgeStore(tmp_path)
    store.enqueue_outbox(request_id="first", target="C1", thread_ts="", chunks=["first"])
    store.enqueue_outbox(request_id="second", target="C1", thread_ts="", chunks=["second"])
    first = store.claim_outbox(lease_seconds=600)
    # The second send of the lane is leased and began its provider call; then its process died.
    write(store.path, "UPDATE outbox SET state='leased', lease_token='dead', lease_until=?, send_started=1 "
                      "WHERE request_id='second'", (time.time() - 1,))
    assert store.claim_outbox() is None  # the first still holds the lane
    assert read(store.path, "SELECT request_id, state, last_error FROM outbox ORDER BY id") == [
        ("first", "leased", ""), ("second", "uncertain", "send_result_unrecorded")]
    store.complete_outbox(first.row_id, first.lease_token, provider_message_ts="1.0")
    assert store.claim_outbox() is None


def test_a_reaction_an_older_version_queued_in_its_channel_lane_moves_even_while_it_waits(tmp_path):
    store = BridgeStore(tmp_path)
    assert store.enqueue_mutation(request_id="react", operation="reaction_add",
                                  payload={"channel": "C1", "ts": "1.0", "name": "eyes"})
    # As 1.9.0 queued it: in the channel's lane, and due later.
    write(store.path, "UPDATE outbox SET ordering_key='C1:', available_at=? WHERE request_id='react'",
          (time.time() + 600,))
    assert store.claim_outbox() is None
    assert read(store.path, "SELECT ordering_key FROM outbox") == [("reaction:C1:1.0",)]


def test_the_kept_connection_commits_with_full_durability(tmp_path):
    store = BridgeStore(tmp_path)
    with store._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_duplicate_mutation_is_not_inserted_on_a_connection_that_already_wrote(tmp_path):
    store = BridgeStore(tmp_path)
    store.set_runtime(workspace_id="T1")
    assert store.enqueue_mutation(request_id="m1", operation="delete_message", payload={"channel": "C1", "ts": "1.0"})
    store.set_runtime(workspace_name="Team")
    assert not store.enqueue_mutation(request_id="m1", operation="delete_message", payload={"channel": "C1", "ts": "1.0"})


def test_a_commit_that_fails_leaves_no_transaction_on_the_kept_connection(tmp_path):
    """Python 3.10 does not roll back a failed commit itself; a per-call connection discarded it on close."""
    store = BridgeStore(tmp_path)
    with store._connect() as db:
        db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE child(id INTEGER PRIMARY KEY, parent INTEGER "
                   "REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as db:
            db.execute("INSERT INTO child(id, parent) VALUES (1, 99)")  # fails only at COMMIT
    with store._connect() as db:
        assert not db.in_transaction
    other = sqlite3.connect(store.path, timeout=0.2, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()
    assert read(store.path, "SELECT COUNT(*) FROM child") == [(0,)]


def test_close_releases_the_file_and_a_later_call_opens_it_again(tmp_path):
    store = BridgeStore(tmp_path)
    store.enqueue_outbox(request_id="r1", target="C1", thread_ts="", chunks=["x"])
    assert Path(f"{store.path}-wal").exists()
    store.close()
    assert store._db is None and not Path(f"{store.path}-wal").exists()
    assert store.claim_outbox().request_id == "r1"


def test_discarding_a_temporary_tool_store_closes_its_connection(tmp_path):
    store = BridgeStore(tmp_path)
    with store._connect() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    del store
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


@pytest.mark.parametrize("failure", [None, "settings", "client", "cleanup"])
def test_the_companion_closes_its_store_on_exit(tmp_path, monkeypatch, failure):
    path = Path(__file__).resolve().parents[1] / "scripts/slack_daemon.py"
    spec = importlib.util.spec_from_file_location("slack_daemon_kept_connection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stores = []

    def opened(directory):
        stores.append(BridgeStore(directory))
        return stores[-1]

    def settings(directory):
        if failure == "settings":
            raise module.LocalSettingsError("settings failed")
        return {}

    class Client:
        def __init__(self, *args):
            if failure == "client":
                raise RuntimeError("client failed")

        async def auth_test(self):
            return {"user_id": "U1", "team_id": "T1"}

        async def aclose(self):
            pass

    class Runtime:
        def __init__(self, **kwargs):
            pass

        async def run(self):
            assert stores[-1]._db is not None

        async def close(self):
            if failure == "cleanup":
                raise RuntimeError("cleanup failed")

    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "BridgeStore", opened)
    monkeypatch.setattr(module, "load_local_settings", settings)
    monkeypatch.setattr(module, "SlackClient", Client)
    monkeypatch.setattr(module, "BridgeRuntime", Runtime)
    monkeypatch.setattr(module, "create_host_adapter", lambda *a, **k: object())

    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *a: None)
        await module._run()

    if failure:
        with pytest.raises((RuntimeError, module.LocalSettingsError), match=f"{failure} failed"):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert len(stores) == 1 and stores[0]._db is None


def test_the_status_route_and_settings_saves_run_beside_the_http_loop(tmp_path, monkeypatch):
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)
    status, _ = api.routes["status"]
    save, _ = api.routes["settings/save"]
    loop_free, threads = threading.Event(), []
    load = module._load_local_settings

    def blocking_load(*args):
        threads.append(threading.get_ident())
        assert loop_free.wait(5), "the HTTP loop was blocked while the route read its files"
        return load(*args)

    monkeypatch.setattr(module, "_load_local_settings", blocking_load)

    async def served(call):
        loop_free.clear()
        route = asyncio.ensure_future(call)
        await asyncio.sleep(0.05)  # the loop keeps turning while the route waits on its read
        loop_free.set()
        return await route

    async def run():
        shown = json.loads((await served(status())).body)
        saved = await served(save(_Request({"SLACK_OUTBOUND_WORKERS": 3})))
        current = json.loads((await served(save(_Request(None, method="GET")))).body)
        return threading.get_ident(), shown, saved, current

    loop_thread, shown, saved, current = asyncio.run(run())
    assert shown["title"] == "Slack transport" and shown["binding_state"] == "missing"
    assert saved.status_code == 200 and current["SLACK_OUTBOUND_WORKERS"] == "3"
    assert threads and loop_thread not in threads


def test_two_settings_saves_at_once_both_keep_their_fields(tmp_path, monkeypatch):
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)
    save, _ = api.routes["settings/save"]
    load = module._load_local_settings

    def slow_load(*args):
        value = load(*args)
        time.sleep(0.1)  # long enough for the other save to read the same file meanwhile
        return value

    monkeypatch.setattr(module, "_load_local_settings", slow_load)

    async def run():
        return await asyncio.gather(save(_Request({"SLACK_OUTBOUND_WORKERS": 3})),
                                    save(_Request({"SLACK_INBOUND_WORKERS": 5})))

    assert [response.status_code for response in asyncio.run(run())] == [200, 200]
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["SLACK_OUTBOUND_WORKERS"] == 3 and saved["SLACK_INBOUND_WORKERS"] == 5
