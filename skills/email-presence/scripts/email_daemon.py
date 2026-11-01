from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lib.client import MailClient
from lib.accounts import AccountError, AccountStore, SETTING_KEYS
from lib.gmail import GmailClient
from lib.host_adapter import create_host_adapter
from lib.runtime import EmailRuntime
from lib.store import EmailStore


def signature(account):
    if not account.get("uses_env"):
        return account["generation"]
    return json.dumps({key: value for key, value in account.items() if key not in {"runtime_error", "_persisted"}}, sort_keys=True)


async def manage_accounts(state, stop, *, refresh_seconds=1):
    """One companion supervises independent mailbox workers; settings reload in place.

    A changed/disconnected account first stops its old worker cooperatively. It
    is not run concurrently with a new credential generation. Other accounts
    remain live while an in-flight provider request finishes.
    """
    instances, failed = {}, {}
    legacy = {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}

    async def worker(accounts, account, own_stop):
        client = host = None
        try:
            directory = accounts.mail_dir(account)
            cfg = account.get("config") or {}
            client = (GmailClient(accounts, account["id"]) if account["provider"] == "gmail" else
                      MailClient({**cfg, "EMAIL_DEFAULT_SENDER": account.get("sender") or account["address"]}))
            root_settings = json.loads((state / "settings.json").read_text(encoding="utf-8")) if (state / "settings.json").exists() else {}
            host = create_host_adapter(str(root_settings.get("binding_id") or "") if account.get("legacy") else "",
                                       words=account.get("words", []), account_id=account["id"], address=account["address"],
                                       senders=account["senders"], sender=account.get("sender") or account["address"])
            runtime = EmailRuntime(EmailStore(directory), client, host, folder=account.get("folder") or "INBOX",
                                   account=account["id"], poll_interval=account.get("poll_interval_sec") or 30,
                                   account_loader=lambda: AccountStore(state, legacy).resolve(account["id"]))
            await runtime.run(own_stop)
        finally:
            if host is not None:
                await host.aclose()
            close = getattr(client, "close", None)
            if close:
                close()

    try:
        while not stop.is_set():
            accounts = AccountStore(state, legacy)
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
        await asyncio.gather(*(entry["task"] for entry in instances.values()), return_exceptions=True)


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
