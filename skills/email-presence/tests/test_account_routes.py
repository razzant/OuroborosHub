import asyncio
import importlib.util
import json
from pathlib import Path

from starlette.testclient import TestClient

from lib.accounts import AccountStore
from test_plugin import _settings_app


def imap_values(address="one@example.invalid"):
    return {"address": address, "imap_host": "imap.example.invalid", "smtp_host": "smtp.example.invalid",
            "password": "private-account-password", "senders": [address], "sending": "off"}


def test_account_routes_redact_credentials_and_status_is_account_specific(tmp_path):
    app = _settings_app(tmp_path)
    with TestClient(app) as http:
        first = http.post("/accounts/save", json=imap_values()).json()["account"]
        aliases = [f"alias-{n}@example.invalid" for n in range(20)]
        second = http.post("/accounts/save", json={**imap_values(aliases[0]), "senders": aliases, "sender": aliases[-1]}).json()["account"]
        assert first["id"] != second["id"]
        a = http.get("/status", params={"account_id": first["id"]}).json()
        b = http.get("/status", params={"account_id": second["id"]}).json()
        assert a["identity"]["sends_as"]["id"] == first["address"]
        assert b["identity"]["sends_as"]["id"] == aliases[-1]
        assert len(b["identity"]["reads_as"]) == 20
        assert b["readiness"]["sending"] == "off"
        assert http.get("/status", params={"account_id": aliases[-1]}).status_code == 404
        public = http.get("/accounts").json()
        assert "private-account-password" not in json.dumps([first, second, a, b, public])
        changed = http.post("/accounts/preferences", json={"account_id": second["id"], "sender": aliases[3], "sending": "on"})
        assert changed.status_code == 200
        assert http.get("/status", params={"account_id": second["id"]}).json()["identity"]["sends_as"]["id"] == aliases[3]
        assert http.post("/accounts/disconnect", json={"account_id": second["id"]}).status_code == 200
        assert http.get("/status", params={"account_id": second["id"]}).json()["connected"] is False
        assert [row["account_id"] for row in http.get("/status").json()["connect"]] == [first["id"]]


def test_connection_edits_preserve_sending_and_words(tmp_path):
    accounts = AccountStore(tmp_path)
    account = accounts.save_imap({**imap_values(), "words": ["invoice", "счёт"], "poll_interval_sec": 75})
    updated = accounts.save_imap({"account_id": account["id"], "address": account["address"],
                                  "imap_host": "imap.example.invalid", "smtp_host": "smtp.example.invalid",
                                  "imap_security": "starttls", "imap_port": 143, "password": ""})
    assert updated["sending"] == "off" and updated["words"] == ["invoice", "счёт"]
    assert updated["poll_interval_sec"] == 75


def test_one_worker_failure_does_not_stop_other_accounts_or_spin_restarts(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/email_daemon.py"
    spec = importlib.util.spec_from_file_location("mail_manager_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    accounts = AccountStore(tmp_path)
    good = accounts.save_imap(imap_values("good@example.invalid"))
    bad = accounts.save_imap(imap_values("bad@example.invalid"))
    attempts, running, closed = [], set(), []
    failure = [True]
    class Client:
        def __init__(self, settings):
            address = settings["EMAIL_USER"]
            attempts.append(address)
            if address == bad["address"] and failure[0]:
                raise RuntimeError("simulated local startup failure")
        def close(self): pass
    class Host:
        async def aclose(self): closed.append(True)
    class Runtime:
        def __init__(self, store, client, host, *, account, **kwargs): self.account = account
        async def run(self, stop):
            running.add(self.account)
            await stop.wait()
            running.remove(self.account)
    monkeypatch.setattr(module, "MailClient", Client)
    monkeypatch.setattr(module, "EmailRuntime", Runtime)
    monkeypatch.setattr(module, "create_host_adapter", lambda *a, **k: Host())
    async def wait_for(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 3)
    async def exercise():
        stop = asyncio.Event()
        task = asyncio.create_task(module.manage_accounts(tmp_path, stop, refresh_seconds=.005))
        try:
            await wait_for(lambda: good["id"] in running and accounts.get(bad["id"]).get("runtime_error"))
            await asyncio.sleep(.03)
            assert attempts.count(bad["address"]) == 1 and good["id"] in running
            failure[0] = False
            accounts.update_preferences(bad["id"], {"label": "retry repaired account"})
            await wait_for(lambda: bad["id"] in running)
            assert good["id"] in running and not closed
        finally:
            stop.set()
            await task
    asyncio.run(exercise())
    assert not running and len(closed) == 2


def test_account_discovery_retains_counts_and_addressable_receipts_after_disconnect(tmp_path):
    from types import SimpleNamespace
    from lib.service import MailService
    from lib.store import EmailStore
    accounts = AccountStore(tmp_path)
    account = accounts.save_imap(imap_values())
    store = EmailStore(accounts.mail_dir(account))
    store.enqueue_outbox(request_id="held-before-disconnect", to=["reader@example.invalid"], body="owed", hold_reason="place_disconnected")
    store.enqueue_outbox(request_id="unknown-acceptance", to=["reader@example.invalid"], body="uncertain")
    item = store.claim_outbox()
    store.uncertain_outbox(item.row_id, item.lease_token, "Provider acceptance unknown")
    store.enqueue_outbox(request_id="failed", to=["reader@example.invalid"], body="failed")
    item = store.claim_outbox()
    store.fail_outbox(item.row_id, item.lease_token, "Provider rejected message")
    accounts.disconnect(account["id"])
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    api_queue = service.list_accounts()["accounts"][0]["queue"]
    assert api_queue["outbox_held"] == 1
    assert api_queue["outbox"]["uncertain"] == api_queue["outbox"]["failed"] == 1
    by_request = {row["request_id"]: row for row in api_queue["receipts"]}
    assert by_request["held-before-disconnect"]["state"] == "held"
    assert by_request["unknown-acceptance"]["state"] == "uncertain"
    assert by_request["failed"]["state"] == "failed"
    assert service.receipt("held-before-disconnect", account_id=account["id"])["state"] == "held"
    with TestClient(_settings_app(tmp_path)) as http:
        web_queue = http.get("/accounts").json()["accounts"][0]["queue"]
        assert web_queue == api_queue
        assert http.get("/status", params={"account_id": account["id"]}).json()["receipts"] == api_queue["receipts"]
