from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lib.client import MailClient
from lib.accounts import AccountError, AccountStore, SETTING_KEYS
from lib.gmail_host import GmailSignIn, sync_gmail
from lib.service import account_client
from lib.host_adapter import create_host_adapter
from lib.runtime import EmailRuntime
from lib.store import EmailStore


def signature(account):
    if not account.get("uses_env"):
        return account["generation"]
    return json.dumps({key: value for key, value in account.items() if key not in {"runtime_error", "_persisted"}}, sort_keys=True)


_SYNC_SECONDS = 15  # between passes that follow the host's Google sign-ins
_BUSY_CODES = (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED: an extended code keeps its primary code in the low byte


def _registry_busy(exc):
    """Another connection holding the registry: the one SQLite failure a later pass may wait out. Python 3.11 names the
    code, an extended variant included; without it only SQLite's own words for those two codes count, never a broken
    statement or a damaged store."""
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in _BUSY_CODES
    words = str(exc)
    return words == "database is locked" or words.startswith(("database table is locked", "database schema is locked"))


async def manage_accounts(state, stop, *, refresh_seconds=1):
    """One companion supervises independent mailbox workers; settings reload in place.

    A changed/disconnected account first stops its old worker cooperatively. It
    is not run concurrently with a new credential generation. Other accounts
    remain live while an in-flight provider request finishes.
    """
    instances, failed = {}, {}
    legacy = {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
    google = GmailSignIn.environment()

    async def follow_google():
        while not stop.is_set():
            try:
                await asyncio.to_thread(sync_gmail, AccountStore(state, legacy, sign_in=google))
            except (OSError, ValueError, sqlite3.OperationalError) as exc:
                if isinstance(exc, sqlite3.OperationalError) and not _registry_busy(exc):
                    raise  # broken SQL or a damaged store ends the companion like any other defect (google_job.result())
                # The host or a state file unreadable, or the registry busy: the next pass reads everything again.
                logging.getLogger("email_presence").warning("Google sign-in state could not be reconciled (%s)",
                                                            type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), _SYNC_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def worker(accounts, account, own_stop):
        client = host = store = None
        try:
            directory = accounts.mail_dir(account)
            client = account_client(accounts, account, imap_client=MailClient)
            root_settings = json.loads((state / "settings.json").read_text(encoding="utf-8")) if (state / "settings.json").exists() else {}
            host = create_host_adapter(str(root_settings.get("binding_id") or "") if account.get("legacy") else "",
                                       words=account.get("words", []), account_id=account["id"], address=account["address"],
                                       senders=account["senders"], sender=account.get("sender") or account["address"])
            store = EmailStore(directory)
            runtime = EmailRuntime(store, client, host, folder=account.get("folder") or "INBOX",
                                   account=account["id"], poll_interval=account.get("poll_interval_sec") or 30,
                                   account_loader=lambda: AccountStore(state, legacy).resolve(account["id"]))
            await runtime.run(own_stop)
        finally:
            try:
                if host is not None:
                    await host.aclose()
            finally:
                try:
                    close = getattr(client, "close", None)
                    if close:
                        close()
                finally:
                    if store is not None:
                        # After its loops and their threads ended: the mailbox's one connection.
                        store.close()

    google_job = asyncio.create_task(follow_google())
    try:
        while not stop.is_set():
            if google_job.done():
                # Any other failure ends the companion with its traceback: the host records the exit and restarts it
                # (restart_policy on_failure), so following the host never stops unseen while mailboxes run on.
                google_job.result()
            accounts = AccountStore(state, legacy, sign_in=google)
            desired = {row["id"]: row for row in accounts.list() if row["connected"]}
            for account_id, entry in list(instances.items()):
                if entry["task"].done():
                    try:
                        entry["task"].result()
                    except Exception as exc:
                        # A programming/store failure does not become an unbounded
                        # per-account restart loop. A new configuration can retry it.
                        failed[account_id] = entry["signature"]
                        accounts.runtime_error(account_id, type(exc).__name__)
                        logging.getLogger("email_presence").error("Mailbox worker stopped: %s (%s)", account_id, type(exc).__name__)
                    del instances[account_id]
                elif account_id not in desired or entry["signature"] != signature(desired[account_id]):
                    entry["stop"].set()
            for account_id, account in desired.items():
                try:
                    account = accounts.resolve(account_id)
                except AccountError:
                    continue  # Disconnected between the snapshot and this iteration.
                current = signature(account)
                if account_id in instances or failed.get(account_id) == current:
                    continue
                own_stop = asyncio.Event()
                accounts.runtime_error(account_id, "")
                instances[account_id] = {"signature": current, "stop": own_stop,
                                         "task": asyncio.create_task(worker(accounts, account, own_stop))}
            try:
                await asyncio.wait_for(stop.wait(), refresh_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        for entry in instances.values():
            entry["stop"].set()
        stop.set()
        await asyncio.gather(google_job, *(entry["task"] for entry in instances.values()), return_exceptions=True)


async def run():
    state = Path(os.environ["OUROBOROS_SKILL_STATE_DIR"])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Host process supervision remains authoritative on Windows.
    await manage_accounts(state, stop)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(run())
