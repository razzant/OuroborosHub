"""One kept mailbox connection: idle loops open no file and take no write lock; FULL durability.

Each mailbox's inbox and outbox loops ask the store every second while nothing waits. Each call
opened a new connection, and each claim began BEGIN IMMEDIATE before it knew there was work.
These guards keep an idle claim a plain read on a connection that stays open, while a claim with
work leases and settles exactly what it did before: a send that may have been accepted becomes
uncertain and is never sent again.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from lib.accounts import AccountStore
from lib.store import EmailStore
from test_account_routes import imap_values


def message(uid, thread=None):
    return {"folder": "INBOX", "uid": uid, "uidvalidity": 1, "message_id": f"<m{uid}@example.invalid>",
            "sender": "reader@example.invalid", "subject": f"Hi {uid}", "body": "Body",
            "references": [thread] if thread else [], "recipients": ["bot@example.invalid"]}


def queue(store, request_id):
    assert store.enqueue_outbox(request_id=request_id, to=["reader@example.invalid"], subject="Re", body=request_id)


def read(path, sql, args=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def idle_poll(store):
    """What a mailbox's idle intake, observe and outbox loops ask each pass."""
    assert store.claim_inbox(submitted=False) is None
    assert store.claim_inbox(submitted=True) is None
    assert store.claim_inbox() is None
    assert store.claim_outbox() is None
    assert store.next_delivery_report() is None


@pytest.mark.parametrize("populated", [False, True])
def test_an_idle_loop_opens_no_file_takes_no_write_lock_and_writes_nothing(tmp_path, monkeypatch, populated):
    store = EmailStore(tmp_path)
    # Rows that are not claimable now: a sent message, a live lease and a retry due later.
    if populated:
        queue(store, "sent")
        sent = store.claim_outbox()
        store.complete_outbox(sent.row_id, sent.lease_token)
        store.ingest(message(1))
        assert store.claim_inbox() is not None
        store.ingest(message(2))
        later = store.claim_inbox()
        store.retry_inbox(later.row_id, later.lease_token, "later", delay=600)
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
    store, rival = EmailStore(tmp_path), EmailStore(tmp_path)
    store.ingest(message(1))
    store.ingest(message(2))
    taken = []

    def rival_claims_first(sql):
        # Runs as our BEGIN IMMEDIATE starts, after our plain read saw the first message free.
        if sql.strip().upper() == "BEGIN IMMEDIATE" and not taken:
            taken.append(rival.claim_inbox().uid)

    with store._connect() as db:
        db.set_trace_callback(rival_claims_first)
    item = store.claim_inbox()
    with store._connect() as db:
        db.set_trace_callback(None)
    assert taken == [1] and item.uid == 2 and item.attempts == 1
    assert store.claim_inbox() is None and rival.claim_inbox() is None
    assert read(store.path, "SELECT uid, state, attempts FROM inbox ORDER BY id") == [
        (1, "processing", 1), (2, "processing", 1)]


@pytest.mark.parametrize("box", ["inbox", "outbox"])
@pytest.mark.parametrize("shared_connection", [False, True])
def test_concurrent_claimers_never_lease_the_same_row(tmp_path, box, shared_connection):
    seed = EmailStore(tmp_path)
    rows = 24
    for index in range(rows):
        if box == "inbox":
            seed.ingest(message(index + 1))
        else:
            queue(seed, f"r{index}")
    stores = [seed] * 3 if shared_connection else [EmailStore(tmp_path) for _ in range(3)]
    claim = (lambda s: s.claim_inbox()) if box == "inbox" else (lambda s: s.claim_outbox())
    start, claimed = threading.Barrier(len(stores)), [[] for _ in stores]

    def claimer(index):
        start.wait()
        while (item := claim(stores[index])) is not None:
            claimed[index].append(item.row_id)

    threads = [threading.Thread(target=claimer, args=(index,)) for index in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert all(not thread.is_alive() for thread in threads)
    every = [item for items in claimed for item in items]
    assert len(every) == len(set(every)) == rows
    assert all(claim(store) is None for store in stores)


def test_a_send_lost_after_sending_began_becomes_uncertain_and_is_never_sent_again(tmp_path):
    """No other row is claimable, so only the read for lost sends can open the sweep."""
    store = EmailStore(tmp_path)
    queue(store, "lost")
    item = store.claim_outbox()
    assert store.mark_sending(item)
    # The process that began SMTP DATA died, and its lease ran out.
    conn = sqlite3.connect(store.path)
    try:
        with conn:
            conn.execute("UPDATE outbox SET lease_until=? WHERE id=?", (time.time() - 1, item.row_id))
    finally:
        conn.close()
    assert store.claim_outbox() is None
    [(state, error)] = read(store.path, "SELECT state, last_error FROM outbox")
    assert state == "uncertain" and error.startswith("Process lost during mail delivery")
    assert store.claim_outbox() is None


def test_the_kept_connection_commits_with_full_durability(tmp_path):
    store = EmailStore(tmp_path)
    with store._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_duplicate_capture_names_no_row_on_a_connection_that_already_inserted(tmp_path):
    store = EmailStore(tmp_path)
    row_id, inserted = store.ingest(message(1))
    assert inserted and row_id
    store.put("poll_health", {"ok": True})
    assert store.ingest(message(1)) == (0, False)


def test_status_reads_its_counts_and_delivery_support_on_the_same_connection(tmp_path):
    store = EmailStore(tmp_path)
    store.put("delivery_support", {"version": 1})
    answer = []
    reader = threading.Thread(target=lambda: answer.append(store.status()), daemon=True)
    reader.start()
    reader.join(5)
    assert answer, "status() waited on its own connection"
    assert answer[0]["delivery_support"] == {"version": 1}


def test_a_commit_that_fails_leaves_no_transaction_on_the_kept_connection(tmp_path):
    """Python 3.10 does not roll back a failed commit itself; a per-call connection discarded it on close."""
    store = EmailStore(tmp_path)
    with store._connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
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
    store = EmailStore(tmp_path)
    queue(store, "r1")
    assert Path(f"{store.path}-wal").exists()
    store.close()
    assert store._db is None and not Path(f"{store.path}-wal").exists()
    assert store.claim_outbox().request_id == "r1"


def test_discarding_a_temporary_tool_store_closes_its_connection(tmp_path):
    store = EmailStore(tmp_path)
    with store._connect() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    del store
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


@pytest.mark.parametrize("failed_cleanup", [None, "host", "client"])
def test_the_companion_closes_a_mailbox_store_when_its_worker_ends(tmp_path, monkeypatch, failed_cleanup):
    path = Path(__file__).resolve().parents[1] / "scripts/email_daemon.py"
    spec = importlib.util.spec_from_file_location("mail_manager_kept_connection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    account = AccountStore(tmp_path).save_imap(imap_values())
    stores = []

    class Client:
        def __init__(self, settings):
            pass

        def close(self):
            if failed_cleanup == "client":
                raise RuntimeError("client close failed")

    class Host:
        async def aclose(self):
            if failed_cleanup == "host":
                raise RuntimeError("host close failed")

    class Runtime:
        def __init__(self, store, client, host, **kwargs):
            stores.append(store)

        async def run(self, stop):
            stores[-1].put("poll_health", {"ok": True})  # the mailbox's connection is open
            await stop.wait()

    monkeypatch.setattr(module, "MailClient", Client)
    monkeypatch.setattr(module, "EmailRuntime", Runtime)
    monkeypatch.setattr(module, "create_host_adapter", lambda *a, **k: Host())

    async def exercise():
        stop = asyncio.Event()
        task = asyncio.create_task(module.manage_accounts(tmp_path, stop, refresh_seconds=.005))
        while not stores or stores[-1]._db is None:
            await asyncio.sleep(.005)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(exercise(), 10))
    assert len(stores) == 1 and stores[0]._db is None
    assert stores[0].path.parent == AccountStore(tmp_path).mail_dir(account)
