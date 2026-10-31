import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from lib.accounts import AccountError, AccountStore
from lib.service import MailService
from lib.store import EmailStore


def gmail_account(store, subject="one", address="one@example.invalid"):
    return store.save({"id": "google:" + subject, "provider": "gmail", "address": address, "label": address,
                       "sender": address, "senders": [address, "alias-" + address], "config": {},
                       "oauth": {"client_id": "fixture-client", "client_secret": "private-client-material"},
                       "tokens": {"access_token": "private-access-material", "refresh_token": "private-refresh-material",
                                  "expires_at": time.time() + 3600},
                       "scopes": ["openid", "email", "https://www.googleapis.com/auth/gmail.modify"],
                       "folder": "INBOX", "sending": "on", "words": [], "poll_interval_sec": 30})


def test_accounts_partition_same_uids_and_keep_credentials_out_of_public_view(tmp_path):
    accounts = AccountStore(tmp_path)
    a, b = gmail_account(accounts), gmail_account(accounts, "two", "two@example.invalid")
    for account in (a, b):
        store = EmailStore(accounts.mail_dir(account))
        assert store.ingest({"uid": 7, "uidvalidity": 3, "message_id": "<same>", "body": account["address"]})[1]
    assert accounts.mail_dir(a) != accounts.mail_dir(b)
    assert EmailStore(accounts.mail_dir(a)).claim_inbox().body == a["address"]
    assert EmailStore(accounts.mail_dir(b)).claim_inbox().body == b["address"]
    public = json.dumps([accounts.public(row) for row in accounts.list()])
    assert "private-access-material" not in public and "private-refresh-material" not in public
    assert "private-client-material" not in public
    assert accounts.path.stat().st_mode & 0o077 == 0
    with pytest.raises(AccountError, match="Choose account_id"):
        accounts.resolve()
    for wrong in (a["address"], a["id"].upper(), a["senders"][1]):
        with pytest.raises(AccountError, match="Unknown account_id"):
            accounts.resolve(wrong)


def test_disconnect_prevents_late_refresh_but_keeps_mail_and_other_account(tmp_path):
    accounts = AccountStore(tmp_path)
    a, b = gmail_account(accounts), gmail_account(accounts, "two", "two@example.invalid")
    store = EmailStore(accounts.mail_dir(a))
    store.enqueue_outbox(request_id="receipt", to=["recipient@example.invalid"], body="retained")
    accounts.disconnect(a["id"])
    assert not accounts.refresh_tokens(a["id"], a["generation"], {"access_token": "late"})
    record = accounts.get(a["id"])
    assert not record["connected"] and "tokens" not in record and "oauth" not in record
    assert accounts.resolve(b["id"])["tokens"]["access_token"] == "private-access-material"
    api = SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda keys: {})
    assert MailService(api).receipt("receipt", account_id=a["id"])["state"] == "pending"
    reconnected = gmail_account(accounts)
    assert reconnected["generation"] != a["generation"]
    assert not accounts.refresh_tokens(a["id"], a["generation"], {"access_token": "late-again"})
    assert store.receipt("receipt")["state"] == "pending"


def test_legacy_account_keeps_original_store_without_copying_env_password(tmp_path):
    legacy = {"EMAIL_USER": "old@example.invalid", "EMAIL_PASSWORD": "private-env-password", "EMAIL_IMAP_HOST": "imap.example.invalid"}
    accounts = AccountStore(tmp_path, legacy)
    original = EmailStore(tmp_path)
    original.enqueue_outbox(request_id="old", to=["recipient@example.invalid"], body="before update")
    a = accounts.resolve()
    assert accounts.mail_dir(a) == tmp_path
    with sqlite3.connect(accounts.path) as db:
        assert "private-env-password" not in db.execute("SELECT record FROM accounts").fetchone()[0]
    assert accounts.resolve()["config"]["EMAIL_PASSWORD"] == "private-env-password"
    changed = AccountStore(tmp_path, {**legacy, "EMAIL_USER": "new@example.invalid"})
    b = changed.resolve()
    assert b["id"] == "new@example.invalid" and changed.mail_dir(b) != tmp_path
    assert changed.get(a["id"])["connected"] is False
    assert original.receipt("old")["provider_message_id"]


def test_imap_login_sender_and_password_update_have_distinct_roles(tmp_path):
    accounts = AccountStore(tmp_path)
    a = accounts.save_imap({"address": "mail@example.invalid", "username": "bridge-login", "password": "private-password",
                            "imap_host": "localhost", "smtp_host": "localhost", "imap_port": 1143, "smtp_port": 1025,
                            "imap_security": "starttls", "senders": "mail@example.invalid\nalias@example.invalid",
                            "sender": "alias@example.invalid"})
    assert a["config"]["EMAIL_USER"] == "bridge-login" and a["sender"] == "alias@example.invalid"
    updated = accounts.save_imap({"account_id": a["id"], "address": a["address"], "username": "bridge-login",
                                  "imap_host": "localhost", "smtp_host": "localhost", "password": ""})
    assert updated["config"]["EMAIL_PASSWORD"] == "private-password"
    with pytest.raises(AccountError, match="different mailbox"):
        accounts.save_imap({"account_id": a["id"], "address": a["address"], "username": "different-login",
                            "imap_host": "localhost", "smtp_host": "localhost", "password": "new"})


def test_request_ids_and_message_refs_are_account_scoped(tmp_path, monkeypatch):
    accounts = AccountStore(tmp_path)
    a, b = gmail_account(accounts), gmail_account(accounts, "two", "two@example.invalid")
    api = SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda keys: {})
    service = MailService(api)
    calls = []
    def bind(account, conversation):
        calls.append(account["id"])
        return {"supported": True, "allowed": True, "binding": {"version": 1, "connector": "email-presence",
                "provider": "email", "account_id": account["id"], "conversation_id": conversation,
                "thread_id": "", "place_id": ""}}
    monkeypatch.setattr(service, "capture_binding", bind)
    ctx = SimpleNamespace(task_id="ordinary-acts-task", task_metadata={"presence": {"event": {"source_event_id": "another-account-source"}}})
    first = service.send(ctx, account_id=a["id"], sender=a["senders"][1], to="recipient@example.invalid", body="hello", request_id="same")
    again = service.send(ctx, account_id=a["id"], sender=a["senders"][1], to="recipient@example.invalid", body="hello", request_id="same")
    other = service.send(ctx, account_id=b["id"], to="recipient@example.invalid", body="hello", request_id="same")
    assert again["deduplicated"] and not other["deduplicated"]
    assert first["receipt"]["provider_message_id"] != other["receipt"]["provider_message_id"]
    assert first["receipt"]["delivery_reporting"]["place_binding"]["place_id"] == ""
    assert first["receipt"]["delivery_reporting"]["origin"]["source_event_id"] == "another-account-source"
    assert calls == [a["id"], a["id"], b["id"]]
    conflict = service.send(account_id=a["id"], sender=a["address"], to="recipient@example.invalid", body="hello", request_id="same")
    assert conflict["code"] == "request_id_conflict"
    reference = service.wrap_ref(a, {"message_ref": "gmail:abc"})
    assert service.unwrap_ref(a, reference) == {"message_ref": "gmail:abc"}
    with pytest.raises(AccountError, match="another account"):
        service.unwrap_ref(b, reference)


def test_new_unconfigured_send_is_not_a_false_queued_success(tmp_path):
    api = SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda keys: {})
    with pytest.raises(AccountError, match="Connect a mailbox"):
        MailService(api).send(to="recipient@example.invalid", body="hello")
    assert not (tmp_path / "email_presence.sqlite3").exists()
