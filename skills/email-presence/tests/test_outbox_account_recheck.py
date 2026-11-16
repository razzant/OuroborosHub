"""Sending off or a disconnect while the provider connection opens holds the same queued item, unsent.

The real AccountStore supplies the account state; the change happens inside the delivery() context, after the
first check and before mark_sending/transmit. Nothing was submitted, so the hold is certain and the item can
be sent later by the same request_id without creating another row.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lib.accounts import AccountStore
from lib.client import MailClient
from lib.runtime import EmailRuntime
from lib.settings import SENDING_OFF_HOLD
from lib.store import EmailStore


def setup(tmp_path, change):
    accounts = AccountStore(tmp_path / "accounts")
    account = accounts.save_imap({"address": "owner@example.invalid", "password": "fixture", "imap_host": "imap.invalid",
                                  "smtp_host": "smtp.invalid"})
    store = EmailStore(tmp_path / "mail")
    store.enqueue_outbox(request_id="one", to=["reader@example.invalid"], body="Hello",
                         reporting={"version": 1, "account_id": account["id"]})
    sent, opened = [], []
    client = MailClient({"EMAIL_USER": "owner@example.invalid"})

    @contextmanager
    def delivery():
        opened.append(True)
        if change:
            change(accounts, account["id"])  # the owner acts while the connection is opening
        yield SimpleNamespace(send_message=lambda message, **_: sent.append(message) or {})
    client.delivery = delivery
    runtime = EmailRuntime(store, client, SimpleNamespace(), account=account["id"],
                           account_loader=lambda: accounts.resolve(account["id"]))
    return runtime, store, accounts, account, sent, opened


def rows(store):
    with store._connect() as db:
        return [dict(row) for row in db.execute("SELECT request_id,state,attempts FROM outbox")]


@pytest.mark.parametrize("change,reason", [
    (lambda accounts, account_id: accounts.update_preferences(account_id, {"sending": "off"}), SENDING_OFF_HOLD),
    (lambda accounts, account_id: accounts.disconnect(account_id), "disconnected"),
])
def test_a_change_during_connection_setup_holds_before_transmit(tmp_path, change, reason):
    runtime, store, accounts, account, sent, opened = setup(tmp_path, change)
    assert runtime.process_outbox()
    assert opened and not sent
    receipt = store.receipt("one")
    assert receipt["state"] == "held" and reason in receipt["last_error"]
    assert rows(store) == [{"request_id": "one", "state": "retry", "attempts": 0}]
    # The same item is released and sent once the account allows it again, without a second row.
    if reason == SENDING_OFF_HOLD:
        accounts.update_preferences(account["id"], {"sending": "on"})
    else:
        with accounts._db(write=True) as db:
            db.execute("UPDATE accounts SET connected=1 WHERE id=?", (account["id"],))
    runtime.client.delivery = lambda: _plain(sent)
    with store._connect() as db:
        db.execute("UPDATE outbox SET available_at=0")
    assert runtime.process_outbox()
    assert store.receipt("one")["state"] == "completed" and len(sent) == 1 and len(rows(store)) == 1


@contextmanager
def _plain(sent):
    yield SimpleNamespace(send_message=lambda message, **_: sent.append(message) or {})


def test_unchanged_account_sends_once(tmp_path):
    runtime, store, _accounts, _account, sent, opened = setup(tmp_path, None)
    assert runtime.process_outbox()
    assert opened and len(sent) == 1 and store.receipt("one")["state"] == "completed"


def test_an_ambiguous_send_is_never_retried(tmp_path):
    runtime, store, _accounts, _account, sent, _opened = setup(tmp_path, None)

    @contextmanager
    def lost():
        def send_message(message, **_):
            sent.append(message)
            raise TimeoutError("connection lost after DATA")
        yield SimpleNamespace(send_message=send_message)
    runtime.client.delivery = lost
    assert runtime.process_outbox()
    assert store.receipt("one")["state"] == "uncertain" and len(sent) == 1
    with store._connect() as db:
        db.execute("UPDATE outbox SET available_at=0")
    assert not runtime.process_outbox() and len(sent) == 1
