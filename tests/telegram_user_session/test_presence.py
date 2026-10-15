"""Wire-level contract tests; no HTTP server, Telegram session or credentials."""

from __future__ import annotations

from collections import deque
import copy
import json
import logging
import pickle
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from userbot.presence import PresenceClient, PresenceToken, build_event


class FakeHost:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []

    def __call__(self, method, path, payload, headers):
        self.calls.append((method, path, payload, headers))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def client(host):
    return PresenceClient("http://127.0.0.1:8765", PresenceToken("fake-skill-token"),
                          "owner-binding", transport=host)


@pytest.mark.parametrize("response,expected", [
    ((200, {"ok": True, "presence_delivery_version": 1}), (1, "available")),
    ((200, {"ok": True}), (0, "host_does_not_advertise_delivery")),
    ((503, {"ok": False}), (0, "identity_unavailable")),
    (TimeoutError("not reached"), (0, "identity_unavailable")),
])
def test_delivery_capability_read_is_bounded_and_not_guessed(response, expected):
    host = FakeHost(response)
    assert client(host).delivery_capability() == expected
    assert host.calls[0][:2] == ("GET", "/identity")
    assert host.calls[0][3] == {"x-skill-token": "fake-skill-token"}


def test_token_only_revealed_for_request_not_client_state_logs_or_serialization(monkeypatch, caplog):
    secret = "fake-skill-token-sensitive"
    monkeypatch.setenv("HOST_SERVICE_TOKEN", secret)
    token = PresenceToken.from_env()
    host = FakeHost((200, {"ok": True, "status": "completed", "outcome": "silent", "text": ""}))
    bridge = PresenceClient("http://127.0.0.1:8765", token, "owner-binding", transport=host)
    assert not hasattr(bridge, "_headers")
    assert bridge._token is token
    assert secret not in repr(token) + str(token) + f"{token}" + repr(bridge)
    assert secret not in json.dumps(bridge.__dict__, default=str)
    with pytest.raises(TypeError):
        json.dumps(token)
    with caplog.at_level(logging.WARNING):
        logging.warning("presence %r, credential %s", bridge, token)
    assert secret not in caplog.text
    for candidate in (token, bridge):
        with pytest.raises((TypeError, pickle.PicklingError)):
            pickle.dumps(candidate)
    with pytest.raises(copy.Error):
        copy.copy(token)
    with pytest.raises(copy.Error):
        copy.deepcopy(token)
    assert bridge.turn(build_event(1, 10, 3, 2, "hi")).kind == "completed"
    assert host.calls[0][3] == {"x-skill-token": secret}
    # Transport exceptions may stringify the request headers; do not carry that
    # untrusted exception text into PresenceResult, logs or repr.
    bad_host = FakeHost(OSError(f"request failed with {secret}"))
    result = PresenceClient("http://127.0.0.1:8765", token, "binding", transport=bad_host).turn(
        build_event(1, 10, 3, 2, "hi"))
    assert result.kind == "retry" and result.code == "transport_unknown"
    assert secret not in repr(result)


def test_raw_token_and_missing_env_are_rejected(monkeypatch):
    with pytest.raises(TypeError, match="opaque"):
        PresenceClient("http://127.0.0.1:8765", "raw-secret", "binding")
    monkeypatch.delenv("HOST_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="required"):
        PresenceToken.from_env()


def test_host_event_shape_scope_and_peer_local_idempotency():
    event = build_event(77, -100456, 42, 1234, "hi", topic_id=89,
                        conversation={"title": "Forum"}, message={"date": "2026-10-12"})
    assert event == {
        "source_event_id": "telegram:77:-100456:89:42", "provider": "telegram",
        "account_id": "77", "conversation_id": "-100456", "thread_id": "89",
        "conversation_key": "telegram:77:-100456:89", "actor": {"platform_actor_id": "1234"},
        "conversation": {"title": "Forum"}, "message": {"message_id": "42", "date": "2026-10-12"},
        "text": "hi",
    }
    assert set(event) == {
        "source_event_id", "provider", "account_id", "conversation_id", "thread_id",
        "conversation_key", "actor", "conversation", "message", "text",
    }
    assert build_event(77, -100457, 42, 1234, "hi", topic_id=89)["source_event_id"] != event["source_event_id"]
    assert build_event(78, -100456, 42, 1234, "hi", topic_id=89)["source_event_id"] != event["source_event_id"]
    assert build_event(77, -100456, 42, 1234, "hi", topic_id=90)["source_event_id"] != event["source_event_id"]
    general = build_event(77, -100456, 42, 1234, "hi")
    assert general["thread_id"] == ""
    assert general["conversation_key"] == "telegram:77:-100456:0"
    assert general["source_event_id"] == "telegram:77:-100456:0:42"
    assert "owner" not in event and "user_id" not in event
    assert build_event(77, -100456, 42, 1234, "hi", message={"message_id": "forged"})[
        "message"]["message_id"] == "42"


def test_edit_revision_is_distinct_stable_and_included_in_host_identity():
    base = dict(account_id=77, peer_id=-100456, message_id=42, actor_id=1234,
                topic_id=89, text="changed", event_kind="edit")
    first = build_event(**base, revision="2026-10-12T21:24:00.123456+00:00")
    replay = build_event(**base, revision="2026-10-12T21:24:00.123456+00:00")
    next_edit = build_event(**base, revision="2026-10-12T21:25:00.123456+00:00")
    original = build_event(77, -100456, 42, 1234, "original", topic_id=89)
    assert first == replay
    assert len({first["source_event_id"], next_edit["source_event_id"],
                original["source_event_id"]}) == 3
    assert first["source_event_id"].endswith(":edit:2026-10-12T21%3A24%3A00.123456%2B00%3A00")
    assert first["message"]["revision"] == "2026-10-12T21:24:00.123456+00:00"
    assert first["message"]["event_kind"] == "edit"
    # Same revision with changed bytes retains its id: Host must reject identity conflict.
    assert build_event(**{**base, "text": "changed again"},
                       revision="2026-10-12T21:24:00.123456+00:00")["source_event_id"] == first["source_event_id"]


@pytest.mark.parametrize("kind,revision", [
    ("edit", ""), ("edit", " 2026-10-12"), ("new", "2026-10-12"), ("delete", ""),
])
def test_edit_revision_rules_refuse_ambiguous_event_id(kind, revision):
    with pytest.raises(ValueError):
        build_event(1, 2, 3, 4, "x", event_kind=kind, revision=revision)


def test_turn_posts_only_host_contract_and_reads_negotiated_result():
    host = FakeHost((200, {
        "ok": True, "status": "completed", "outcome": "message", "text": "Reply",
        "turn_ref": "turn-1", "work_ref": "", "delivery_reporting_version": 1,
    }))
    event = build_event(77, -100456, 42, 1234, "Hello", topic_id=89)
    result = client(host).turn(event, delivery_reporting_version=1)
    assert (result.kind, result.outcome, result.text, result.turn_ref, result.delivery_reporting_version) == (
        "completed", "message", "Reply", "turn-1", 1,
    )
    method, path, payload, headers = host.calls[0]
    assert (method, path) == ("POST", "/presence/turn")
    assert payload == {"binding_id": "owner-binding", "event": event, "delivery_reporting_version": 1}
    assert headers == {"x-skill-token": "fake-skill-token"}
    assert "/chat/inject" not in path


@pytest.mark.parametrize("outcome", ["deferred", "silent", "tool_delivered"])
def test_completed_host_outcome_not_confused_with_transport_retry(outcome):
    host = FakeHost((200, {"ok": True, "status": "completed", "outcome": outcome,
                           "text": "" if outcome == "silent" else "queued", "turn_ref": "turn-1",
                           "work_ref": "work-1", "delivery_reporting_version": 0}))
    result = client(host).turn(build_event(1, 10, 3, 2, "hi"))
    assert result.kind == "completed" and result.outcome == outcome and result.work_ref == "work-1"


@pytest.mark.parametrize("error,expected", [
    ((503, {"ok": False, "code": "presence_turn_not_started", "disposition": "retry", "error": "retry"}), "retry"),
    ((409, {"ok": False, "code": "presence_event_identity_conflict", "disposition": "rejected", "error": "different"}), "rejected"),
    ((403, {"ok": False, "code": "presence_auth_blocked", "disposition": "blocked", "error": "no token"}), "blocked"),
    (TimeoutError("unknown POST outcome"), "retry"),
    ((200, {"ok": True, "status": "completed", "outcome": "???", "text": "x"}), "retry"),
])
def test_host_disposition_and_ambiguous_transport_are_not_success(error, expected):
    host = FakeHost(error)
    result = client(host).turn(build_event(1, 10, 3, 2, "hi"))
    assert result.kind == expected
    assert result.kind != "completed"


def test_late_work_is_correlated_without_new_turn():
    host = FakeHost(
        (202, {"ok": True, "status": "pending", "work_ref": "work-1"}),
        (200, {"ok": True, "status": "completed", "outcome": "message", "text": "done",
               "work_ref": "work-1", "delivery_reporting_version": 1}),
    )
    bridge = client(host)
    assert bridge.work("work-1").kind == "pending"
    assert bridge.work("work-1").text == "done"
    assert all(method == "GET" and path == "/presence/work/work-1?binding_id=owner-binding"
               for method, path, _, _ in host.calls)


def test_delivery_only_on_concrete_provider_receipt_and_negotiated_version():
    host = FakeHost((200, {"ok": True, "recorded": True, "duplicate": False,
                           "history_coverage": "indexed"}))
    bridge = client(host)
    params = dict(delivery_id="intent-1", part_id="0", account_id=77,
                  peer_id=-100456, topic_id=89, text="Reply", task_id="turn-1",
                  source_event_id="telegram:77:-100456:89:42", delivery_reporting_version=1)
    with pytest.raises(ValueError):
        bridge.report_delivered(**params, provider_message_id=0)
    with pytest.raises(ValueError):
        bridge.report_delivered(**{**params, "delivery_reporting_version": 0}, provider_message_id=999)
    assert not host.calls
    result = bridge.report_delivered(**params, provider_message_id=999)
    assert result.kind == "completed"
    method, path, receipt, headers = host.calls[0]
    assert (method, path, headers) == ("POST", "/presence/delivery", {"x-skill-token": "fake-skill-token"})
    assert receipt == {
        "schema_version": 1, "delivery_id": "intent-1", "part_id": "0", "state": "delivered",
        "provider": "telegram", "account_id": "77", "conversation_id": "-100456", "thread_id": "89",
        "text": "Reply", "format": "text", "message": {"message_id": "999"},
        "origin": {"kind": "automatic", "task_id": "turn-1",
                   "source_event_id": "telegram:77:-100456:89:42"},
    }


def test_failed_receipt_write_retries_same_fact_and_not_claimed_success():
    host = FakeHost((503, {"ok": False, "error": "write failed", "code": "presence_receipt_unwritable",
                           "disposition": "retry"}))
    assert client(host).report_delivered(delivery_id="intent-1", part_id="0", provider_message_id=2,
                                          account_id=77, peer_id=15, text="sent",
                                          delivery_reporting_version=1).kind == "retry"


@pytest.mark.parametrize("kwargs", [
    {"account_id": True}, {"peer_id": 0}, {"message_id": 0}, {"topic_id": -1}, {"actor_id": 0},
])
def test_invalid_identity_is_refused_before_http(kwargs):
    args = dict(account_id=1, peer_id=2, message_id=3, actor_id=4, text="hello")
    args.update(kwargs)
    with pytest.raises(ValueError):
        build_event(**args)


def test_default_http_transport_refuses_non_loopback_and_credentials():
    for url in ("https://127.0.0.1:8765", "http://localhost:8765@evil.test",
                "http://192.168.1.2:8765", "http://localhost:8765/other"):
        with pytest.raises(ValueError):
            PresenceClient(url, PresenceToken("token"), "binding")


def test_host_client_disables_environment_proxy_even_for_loopback(monkeypatch):
    from urllib import request as urllib_request
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted-proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    original = urllib_request.build_opener
    captured = []
    def record(*handlers):
        captured.extend(handlers)
        return original(*handlers)
    monkeypatch.setattr(urllib_request, "build_opener", record)
    PresenceClient("http://127.0.0.1:8767", PresenceToken("private"), "binding")
    proxies = [item.proxies for item in captured if isinstance(item, urllib_request.ProxyHandler)]
    assert proxies == [{}]


def test_host_token_is_not_forwarded_to_redirect_target():
    received = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.headers.get("x-skill-token"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"status":"completed","outcome":"silent","text":""}')
        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Target) as target:
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
                self.end_headers()
            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Redirect) as source:
            threads = [threading.Thread(target=s.serve_forever, daemon=True)
                       for s in (source, target)]
            for thread in threads:
                thread.start()
            try:
                bridge = PresenceClient(f"http://127.0.0.1:{source.server_port}",
                                        PresenceToken("only-loopback-host"), "binding", timeout=2)
                assert bridge.turn(build_event(1, 10, 3, 2, "hi")).kind == "retry"
                assert received == []
            finally:
                source.shutdown()
                target.shutdown()
                for thread in threads:
                    thread.join(timeout=3)
