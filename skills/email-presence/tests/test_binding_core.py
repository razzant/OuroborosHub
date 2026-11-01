"""Mail enqueue and delivery consume the real core Place binding endpoint.

Set OUROBOROS_SOURCE_DIR to the coordinated core candidate. Provider delivery is
an offline recorder; the host's scope resolution and retained binding are real.
"""
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from lib.accounts import AccountStore
from lib.client import MailClient
from lib.host_adapter import LoopbackPresenceHostAdapter
from lib.runtime import EmailRuntime
from lib.service import MailService
from lib.store import EmailStore


@pytest.fixture
def core_host(tmp_path, monkeypatch):
    source = os.environ.get("OUROBOROS_SOURCE_DIR")
    if not source:
        pytest.skip("Set OUROBOROS_SOURCE_DIR to exercise the real core binding contract")
    monkeypatch.syspath_prepend(str(Path(source).resolve()))
    from ouroboros import places
    from ouroboros.gateway import host_service
    drive = tmp_path / "host"
    drive.mkdir()
    async def authenticated(*_):
        return "email-presence", {}
    monkeypatch.setattr(host_service, "_authenticated", authenticated)
    with TestClient(host_service.create_host_service_app(drive)) as http:
        adapter = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767",
                                             skill_token="fixture", place_http_client=http, sync_only=True)
        yield places, drive, adapter


class DeliveryClient(MailClient):
    def __init__(self, settings):
        super().__init__(settings)
        self.sent = []
    @contextmanager
    def delivery(self):
        yield self
    def transmit(self, session, message, recipients, **_):
        self.sent.append((str(message["From"]), list(recipients), str(message["Message-ID"])))
        return {}, {"transport": "offline"}


def mail_context(tmp_path, adapter):
    state = tmp_path / "mail"
    accounts = AccountStore(state)
    first = accounts.save_imap({"address": "first@example.invalid", "password": "fixture", "imap_host": "imap.invalid", "smtp_host": "smtp.invalid"})
    second = accounts.save_imap({"address": "second@example.invalid", "password": "fixture", "imap_host": "imap.invalid", "smtp_host": "smtp.invalid",
                                 "senders": ["second@example.invalid", "alias@example.invalid"]})
    api = SimpleNamespace(get_state_dir=lambda: state, get_settings=lambda _: {})
    service = MailService(api, mail_client=DeliveryClient)
    service.capture_binding = lambda account, conversation: adapter.capture_binding(account["id"], conversation)
    store = EmailStore(accounts.mail_dir(second))
    delivery = DeliveryClient({"EMAIL_USER": second["address"]})
    runtime = EmailRuntime(store, delivery, adapter, account=second["id"], account_loader=lambda: accounts.resolve(second["id"]))
    return accounts, first, second, service, store, delivery, runtime


def release_hold(store):
    with store._connect() as db:
        db.execute("UPDATE outbox SET available_at=0 WHERE state='retry'")


def test_unbound_account_can_send_but_later_silent_scope_holds_same_receipt(tmp_path, core_host):
    places, drive, adapter = core_host
    accounts, first, second, service, store, delivery, runtime = mail_context(tmp_path, adapter)
    places.connect_place(drive, connector="email-presence", provider="email", account_id=first["id"], state="silent")
    sent = service.send(account_id=second["id"], sender="alias@example.invalid", to="reader@example.invalid", body="first", request_id="first")
    assert sent["receipt"]["delivery_reporting"]["place_binding"]["place_id"] == ""
    assert runtime.process_outbox() and len(delivery.sent) == 1
    assert store.receipt("first")["state"] == "completed"
    held = service.send(account_id=second["id"], to="reader@example.invalid", body="second", request_id="second")
    own = places.connect_place(drive, connector="email-presence", provider="email", account_id=second["id"], state="silent")
    assert runtime.process_outbox()
    assert store.receipt("second")["state"] == "held" and len(delivery.sent) == 1
    places.edit_place(drive, own.place_id, "set_state", author="owner", state="active")
    release_hold(store)
    assert runtime.process_outbox()
    assert store.receipt("second")["state"] == "completed" and len(delivery.sent) == 2
    assert held["receipt"]["provider_message_id"] == delivery.sent[-1][2]


def test_disconnect_never_reclassifies_held_bound_send_or_automatic_reply_as_unbound(tmp_path, core_host):
    places, drive, adapter = core_host
    accounts, first, second, service, store, delivery, runtime = mail_context(tmp_path, adapter)
    own = places.connect_place(drive, connector="email-presence", provider="email", account_id=second["id"], state="silent")
    queued = service.send(account_id=second["id"], to="reader@example.invalid", body="held", request_id="held")
    bound = queued["receipt"]["delivery_reporting"]["place_binding"]
    assert bound["place_id"] == own.place_id and queued["state"] == "held"
    store.ingest({"uid": 1, "uidvalidity": 1, "folder": "INBOX", "message_id": "<incoming>",
                  "sender": "reader@example.invalid", "subject": "Incoming", "body": "question"})
    item = store.claim_inbox()
    runtime._reply(item, ("automatic answer",), "output-one")
    automatic_id = f"presence:{item.provider_event_key}:output-one"
    assert store.receipt(automatic_id)["delivery_reporting"]["place_binding"]["place_id"] == own.place_id
    places.edit_place(drive, own.place_id, "disconnect", author="owner")
    # Reconnecting an account must not release mail held under the disconnected binding.
    places.connect_place(drive, connector="email-presence", provider="email", account_id=second["id"], state="active")
    release_hold(store)
    for _ in range(2):
        assert runtime.process_outbox()
    assert not delivery.sent
    assert "place_disconnected" in store.receipt("held")["last_error"]
    assert store.receipt("held")["delivery_reporting"]["place_binding"] == bound
    assert store.receipt(automatic_id)["state"] == "held"
    # A newly requested operation binds the new Place and remains usable.
    fresh = service.send(account_id=second["id"], to="reader@example.invalid", body="new request", request_id="fresh")
    assert fresh["receipt"]["delivery_reporting"]["place_binding"] != bound
    assert runtime.process_outbox() and len(delivery.sent) == 1
    assert store.receipt("fresh")["state"] == "completed"


def test_unconnected_intake_skips_provider_without_losing_explicit_tools(tmp_path, core_host):
    _places, _drive, adapter = core_host
    _accounts, _first, second, _service, store, delivery, runtime = mail_context(tmp_path, adapter)
    def provider_must_not_open(*_, **__):
        pytest.fail("An account without an intake Place opened the provider poll")
    delivery.imap = provider_must_not_open
    assert runtime.poll() == 0
    assert store.get("poll_health")["intake"] == "no_place"
    assert adapter.capture_binding(second["id"], "<chosen-send>")["allowed"]


def test_known_email_thread_is_preserved_in_intake_and_automatic_binding(tmp_path, core_host):
    from dataclasses import replace
    places, drive, adapter = core_host
    _accounts, _first, second, _service, store, delivery, runtime = mail_context(tmp_path, adapter)
    connected = places.connect_place(drive, connector="email-presence", provider="email", account_id=second["id"],
                                     conversation_id="<thread>", state="active")
    places._write(drive, connected.place_id, lambda row: replace(row, thread_id="<thread>"))
    provider_calls = []
    def poll(_store, *, folder, accepts):
        provider_calls.append(folder)
        message = {"uid": 1, "uidvalidity": 1, "folder": "INBOX", "message_id": "<incoming>",
                   "references": ["<thread>"], "sender": "reader@example.invalid", "body": "thread message"}
        assert accepts(message), "The known RFC thread must reach its connected Place"
        store.ingest(message)
        return 1
    delivery.poll = poll
    assert runtime.poll() == 1
    assert provider_calls == ["INBOX"]
    item = store.claim_inbox()
    runtime._reply(item, ("thread answer",), "thread-output")
    queued = store.receipt(f"presence:{item.provider_event_key}:thread-output")
    binding = queued["delivery_reporting"]["place_binding"]
    assert binding["place_id"] == connected.place_id
    assert binding["conversation_id"] == binding["thread_id"] == "<thread>"
    assert runtime.process_outbox() and len(delivery.sent) == 1


def test_ordinary_reply_preserves_known_thread_and_rejects_an_unknown_thread(tmp_path, core_host, monkeypatch):
    from dataclasses import replace
    import lib.service as service_module
    places, drive, adapter = core_host
    _accounts, _first, account, service, store, delivery, runtime = mail_context(tmp_path, adapter)
    del service.capture_binding
    service.api.get_skill_token = lambda: "fixture"
    monkeypatch.setattr(service_module, "LoopbackPresenceHostAdapter", lambda *_, **__: SimpleNamespace(
        capture_binding=adapter.capture_binding, close_sync=lambda: None))
    place = places.connect_place(drive, connector="email-presence", provider="email", account_id=account["id"],
                                 conversation_id="<thread>", state="active")
    places._write(drive, place.place_id, lambda row: replace(row, thread_id="<thread>"))
    good = service.send(account_id=account["id"], to="reader@example.invalid", body="answer", request_id="known",
                        reply_to_message_id="<incoming>", references=["<thread>"])
    assert good["state"] != "held"
    assert runtime.process_outbox() and len(delivery.sent) == 1
    wrong = service.send(account_id=account["id"], to="reader@example.invalid", body="wrong thread", request_id="unknown",
                         reply_to_message_id="<other>", references=["<unknown-thread>"])
    assert wrong["state"] == "held"
    assert wrong["receipt"]["delivery_reporting"]["place_binding"] is None
    release_hold(store)
    assert runtime.process_outbox() and len(delivery.sent) == 1


@pytest.mark.parametrize("failure", ["timeout", "503"])
def test_capture_transport_failure_retries_paid_output_without_resubmitting_event(tmp_path, core_host, monkeypatch, failure):
    import asyncio
    import httpx
    from lib.host_adapter import HostDelivery, HostOutput, HostTurnStatus
    places, drive, adapter = core_host
    _accounts, _first, account, _service, store, delivery, runtime = mail_context(tmp_path, adapter)
    places.connect_place(drive, connector="email-presence", provider="email", account_id=account["id"], state="active")
    source = {"uid": 1, "uidvalidity": 1, "message_id": "<incoming>", "sender": "reader@example.invalid", "body": "question"}
    store.ingest(source)
    submissions, captures = [], []
    real_capture = adapter.capture_binding
    async def submit(item):
        submissions.append(item.provider_event_key)
        return "cached-turn"
    async def status(_):
        return HostTurnStatus("ready", outputs=(HostOutput("paid-output", "paid answer", "producer"),))
    async def deliver(_):
        return HostDelivery(())
    async def support():
        return 0
    def capture(*args, **kwargs):
        captures.append(args)
        if len(captures) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("temporary host timeout")
            response = httpx.Response(503, request=httpx.Request("GET", "http://127.0.0.1/presence/place"))
            response.raise_for_status()
        return real_capture(*args, **kwargs)
    monkeypatch.setattr(adapter, "submit", submit)
    monkeypatch.setattr(adapter, "status", status)
    monkeypatch.setattr(adapter, "deliver", deliver)
    monkeypatch.setattr(adapter, "discover_delivery_support", support)
    monkeypatch.setattr(adapter, "capture_binding", capture)
    adapter.available = True
    asyncio.run(runtime.process_inbox())
    assert store.status()["inbox"] == {"retry": 1}
    assert store.status()["outbox"] == {}, "Transport failure before enqueue must not become an immutable null binding"
    with store._connect() as db:
        assert db.execute("SELECT host_reference FROM inbox").fetchone()[0] == "cached-turn"
        db.execute("UPDATE inbox SET available_at=0")
    asyncio.run(runtime.process_inbox())
    assert store.status()["inbox"] == {"completed": 1}
    assert len(submissions) == 1 and len(captures) == 2
    assert runtime.process_outbox() and len(delivery.sent) == 1
    assert not runtime.process_outbox()
    assert store.status()["outbox"] == {"completed": 1}


def test_typed_unknown_binding_remains_held_and_output_replay_never_recaptures(tmp_path, core_host, monkeypatch):
    places, drive, adapter = core_host
    _accounts, _first, account, _service, store, delivery, runtime = mail_context(tmp_path, adapter)
    places.connect_place(drive, connector="email-presence", provider="email", account_id=account["id"], conversation_id="<different>")
    store.ingest({"uid": 1, "uidvalidity": 1, "message_id": "<incoming>", "sender": "reader@example.invalid", "body": "question"})
    item = store.claim_inbox()
    runtime._reply(item, ("retained answer",), "stable")
    request_id = f"presence:{item.provider_event_key}:stable"
    before = store.receipt(request_id)
    assert before["state"] == "held" and before["delivery_reporting"]["place_binding"] is None
    monkeypatch.setattr(adapter, "capture_binding", lambda *_, **__: pytest.fail("Existing output binding must not be recaptured"))
    runtime._reply(item, ("retained answer",), "stable")
    release_hold(store)
    assert runtime.process_outbox() and not delivery.sent
    assert store.receipt(request_id)["delivery_reporting"]["place_binding"] is None


def test_policy_hold_avoids_provider_login_and_rechecks_after_allowed_login(tmp_path, core_host):
    places, drive, adapter = core_host
    _accounts, _first, account, service, store, delivery, runtime = mail_context(tmp_path, adapter)
    place = places.connect_place(drive, connector="email-presence", provider="email", account_id=account["id"], state="silent")
    service.send(account_id=account["id"], to="reader@example.invalid", body="held", request_id="held")
    logins = []
    flip_after_login = [False]
    @contextmanager
    def login():
        logins.append(True)
        if flip_after_login[0]:
            places.edit_place(drive, place.place_id, "set_state", author="owner", state="silent")
        yield delivery
    delivery.delivery = login
    for _ in range(3):
        release_hold(store)
        assert runtime.process_outbox()
    assert not logins and not delivery.sent
    places.edit_place(drive, place.place_id, "set_state", author="owner", state="active")
    flip_after_login[0] = True
    release_hold(store)
    assert runtime.process_outbox()
    assert len(logins) == 1 and not delivery.sent
    assert store.receipt("held")["state"] == "held"
    flip_after_login[0] = False
    places.edit_place(drive, place.place_id, "set_state", author="owner", state="active")
    release_hold(store)
    assert runtime.process_outbox()
    assert len(logins) == 2 and len(delivery.sent) == 1
