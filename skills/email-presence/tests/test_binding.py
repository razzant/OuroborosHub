import json

import httpx
import pytest

from lib.host_adapter import LoopbackPresenceHostAdapter


class OpaqueToken:
    def __init__(self): self.uses = 0
    def __str__(self): raise AssertionError("Do not stringify an opaque skill token")
    def use_in_request(self):
        self.uses += 1
        return "fixture-token"


def test_bind_check_retains_original_target_and_uses_opaque_token_only_for_requests():
    token = OpaqueToken()
    calls = []
    state = ["active"]
    def handler(request):
        calls.append(request)
        assert request.headers["X-Skill-Token"] == "fixture-token"
        if request.url.path == "/identity":
            return httpx.Response(200, json={"places_version": 1, "presence_delivery_version": 1, "presence_place_binding_version": 1})
        params = request.url.params
        if params["operation"] == "bind":
            assert params["require_place"] == "true"
            binding = {"version": 1, "connector": "email-presence", "provider": "email", "account_id": params["account_id"],
                       "conversation_id": params["conversation_id"], "thread_id": params["thread_id"], "place_id": "original-place"}
            return httpx.Response(200, json={"ok": True, "allowed": True, "binding": binding, "state": "active"})
        binding = json.loads(params["binding"])
        assert binding["place_id"] == "original-place"
        assert params["account_id"] == binding["account_id"] == "google:canonical"
        assert params["conversation_id"] == binding["conversation_id"] == "<thread>"
        return httpx.Response(200, json={"ok": True, "allowed": state[0] == "active", "binding": binding,
                                         "state": state[0], "code": "place_disconnected" if state[0] != "active" else ""})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token=token,
                                           place_http_client=http, sync_only=True)
        binding = host.capture_binding("google:canonical", "<thread>", require_place=True)["binding"]
        assert host.binding_hold(binding, "google:canonical", "<thread>") == ""
        state[0] = "disconnected"
        assert host.binding_hold(binding, "google:canonical", "<thread>") == EMAIL_WAS_DISCONNECTED
        assert host.binding_hold(None, "google:canonical", "<thread>") == NO_PLACE_WHEN_QUEUED
    assert len(calls) == token.uses == 4


PLACE_POLICY_WORDS = ("Held: Ouroboros did not answer whether this place allows sending. "
                      "Nothing was sent yet; it retries by itself and no approval is needed.")
NO_PLACE_COVERS = "Held: no connected place covers this send; nothing was sent."
# Captured without a binding: nothing can release this send later.
NO_PLACE_WHEN_QUEUED = ("Held: no connected place covered this send when it was queued, so it will not be sent; "
                        "send it again once a place covers it.")
# Removed, or disconnected and connected again: the Host never releases this binding.
EMAIL_WAS_DISCONNECTED = ("Held: Email was disconnected after this send was queued, so it will not be sent; "
                          "send it again if it is still needed.")
BINDING = {"version": 1, "connector": "email-presence", "provider": "email", "account_id": "account",
           "conversation_id": "<thread>", "thread_id": "", "place_id": "bound"}


def _hold(answer, check=lambda host: host.binding_hold(BINDING, "account", "<thread>")):
    """A real adapter whose Host answers every Place read as told; an exception is raised."""
    def handler(request):
        if request.url.path == "/identity":
            return httpx.Response(200, json={"places_version": 1, "presence_delivery_version": 1, "presence_place_binding_version": 1})
        if isinstance(answer, BaseException):
            raise answer
        return answer
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        return check(LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="fixture",
                                                 place_http_client=http, sync_only=True))


@pytest.mark.parametrize("answer", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
    httpx.Response(503, json={"ok": False, "error": "disk", "code": "places_unavailable", "disposition": "retry"}),
    httpx.Response(400, json={"ok": False, "code": "place_binding_invalid", "disposition": "rejected"}),
    httpx.Response(500, text="not JSON"),
    httpx.Response(200, json=["not an object"]),
    httpx.Response(200, json={"ok": True, "allowed": False}),
    httpx.Response(200, json={"ok": True, "allowed": False, "code": "place_something_new"}),
])
def test_unread_binding_is_held_in_words_that_ask_nothing_and_print_no_code(answer):
    assert _hold(answer) == PLACE_POLICY_WORDS


@pytest.mark.parametrize("facts,words", [
    ({"state": "silent", "code": "place_silent", "label": "Work mail"}, "Held: Work mail is silent"),
    ({"state": "paused", "code": "place_paused", "label": "Work mail"}, "Held: Work mail is paused"),
    ({"state": "disconnected", "code": "place_disconnected", "label": ""}, EMAIL_WAS_DISCONNECTED),
    ({"state": "disconnected", "code": "place_disconnected", "label": "Work mail"},
     "Held: Work mail was disconnected after this send was queued, so it will not be sent; "
     "send it again if it is still needed."),
    # The Host answered no: "did not answer" and "no approval" would be false here.
    ({"state": "changed", "code": "place_scope_changed", "label": "Work mail"}, NO_PLACE_COVERS),
    ({"state": "unknown", "code": "place_unattributable", "label": ""}, NO_PLACE_COVERS),
    ({"state": "unknown", "code": "place_not_connected", "label": ""}, NO_PLACE_COVERS),
])
def test_a_refusal_names_the_place_and_never_its_code(facts, words):
    assert _hold(httpx.Response(200, json={"ok": True, "allowed": False, "binding": BINDING, **facts})) == words


def test_refused_authorization_keeps_its_own_words():
    assert _hold(httpx.Response(403, json={"code": "presence_auth_blocked"})) == "Held: host authorization blocked"


@pytest.mark.parametrize("answer,words", [
    (httpx.ConnectError("refused"), PLACE_POLICY_WORDS),
    (httpx.Response(503, json={"ok": False, "code": "places_unavailable", "disposition": "retry"}), PLACE_POLICY_WORDS),
    (httpx.Response(404, json={"ok": False, "code": "place_gone"}), PLACE_POLICY_WORDS),
    (httpx.Response(200, json={"place_id": "p", "label": "Email"}), PLACE_POLICY_WORDS),
    (httpx.Response(200, json={"state": "silent", "label": "Work mail"}), "Held: Work mail is silent"),
    (httpx.Response(200, json={"state": "paused"}), "Held: Email is paused"),
    (httpx.Response(200, json={"state": "active"}), ""),
])
def test_legacy_check_reads_a_place_state_as_before_and_an_unread_one_in_the_same_words(answer, words):
    assert _hold(answer, lambda host: host.place_hold("account", "<thread>")) == words


@pytest.mark.parametrize("facts,words", [
    ({"state": "paused", "code": "place_paused", "label": "Work mail"}, "Held: Work mail is paused"),
    # The Host captures no binding for a send no place covers.
    ({"state": "unknown", "code": "place_unattributable", "label": "", "binding": None}, NO_PLACE_WHEN_QUEUED),
    ({"state": "unknown", "code": "place_not_connected", "label": "", "binding": None}, NO_PLACE_WHEN_QUEUED),
    ({"code": "place_something_new"}, PLACE_POLICY_WORDS),
    ({}, PLACE_POLICY_WORDS),
])
def test_enqueue_holds_use_the_same_words_and_count_as_held(tmp_path, monkeypatch, facts, words):
    """Both producers that hold a send before it is queued: the tool and an automatic reply."""
    from types import SimpleNamespace

    from lib.accounts import AccountStore
    from lib.runtime import EmailRuntime
    from lib.service import MailService
    from lib.store import EmailStore, InboxItem
    accounts = AccountStore(tmp_path)
    account = accounts.save_imap({"address": "me@example.invalid", "password": "fixture", "imap_host": "imap.invalid", "smtp_host": "smtp.invalid"})
    refused = {"supported": True, "allowed": False, "binding": BINDING, **facts}
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    monkeypatch.setattr(service, "capture_binding", lambda *_: refused)
    assert service.send(account_id=account["id"], to="reader@example.invalid", body="owed", request_id="tool")["state"] == "held"

    store = EmailStore(accounts.mail_dir(account))
    host = SimpleNamespace(capture_binding=lambda *_, **__: refused)
    runtime = EmailRuntime(store, SimpleNamespace(settings={}), host, account=account["id"])
    item = InboxItem(1, "lease", "INBOX", 4, "<incoming>", "", (), "reader@example.invalid", "Subject", "Text", (), "", 1)
    runtime._reply(item, ("answer",), "output")

    tool, automatic = store.receipt("tool"), store.receipt(f"presence:{item.provider_event_key}:output")
    assert tool["last_error"] == automatic["last_error"] == words
    assert automatic["state"] == "held" and store.status()["outbox_held"] == 2


def test_a_send_no_place_covered_reads_the_same_after_the_worker_rechecks_it(tmp_path, monkeypatch):
    """The tool's answer and the receipt after a worker pass say one sentence for one fact."""
    from types import SimpleNamespace

    from lib.accounts import AccountStore
    from lib.runtime import EmailRuntime
    from lib.service import MailService
    from lib.store import EmailStore
    accounts = AccountStore(tmp_path)
    account = accounts.save_imap({"address": "me@example.invalid", "password": "fixture", "imap_host": "imap.invalid", "smtp_host": "smtp.invalid"})
    refused = {"supported": True, "ok": True, "allowed": False, "binding": None, "state": "unknown",
               "code": "place_unattributable", "label": "", "place_id": ""}
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    monkeypatch.setattr(service, "capture_binding", lambda *_: refused)
    answer = service.send(account_id=account["id"], to="reader@example.invalid", body="owed", request_id="tool")
    assert answer["state"] == "held" and answer["receipt"]["last_error"] == NO_PLACE_WHEN_QUEUED

    store = EmailStore(accounts.mail_dir(account))
    with store._connect() as db:
        db.execute("UPDATE outbox SET available_at=0 WHERE state='retry'")
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Nothing to recheck without a binding"))) as http:
        host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="fixture",
                                           place_http_client=http, sync_only=True)
        assert EmailRuntime(store, SimpleNamespace(settings={}), host, account=account["id"]).process_outbox()
    receipt = store.receipt("tool")
    assert receipt["state"] == "held" and receipt["last_error"] == NO_PLACE_WHEN_QUEUED


def test_legacy_capability_does_not_invent_unbound_snapshot_or_repeat_identity_probe():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == "/identity"
        return httpx.Response(200, json={"presence_delivery_version": 1, "places_version": 1})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="fixture",
                                           place_http_client=http, sync_only=True)
        assert host.capture_binding("account", "") == {"supported": False}
        assert host.capture_binding("account", "") == {"supported": False}
    assert len(calls) == 1


def test_legacy_no_place_is_distinct_from_auth_and_outage():
    def handler(request):
        if request.url.path == "/identity":
            return httpx.Response(200, json={"presence_delivery_version": 1, "places_version": 1})
        return httpx.Response(404, json={"code": "place_not_connected", "disposition": "blocked"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="fixture",
                                           place_http_client=http, sync_only=True)
        assert "no connected Place" in host.place_hold("account", "<thread>")
