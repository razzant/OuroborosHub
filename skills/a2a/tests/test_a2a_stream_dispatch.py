"""Presence dispatch, receipts, replay and honest wait expiration."""
import importlib.util
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def transport(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/a2a_presence.py"
    spec = importlib.util.spec_from_file_location("a2a_presence_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    posts, gets = [], []
    class HTTP:
        response = {"status": "completed", "outcome": "message", "text": "answer", "turn_ref": "turn"}
        status = 200
        def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return httpx.Response(self.status, json=self.response, request=httpx.Request("POST", url))
        def get(self, url, **kwargs):
            gets.append((url, kwargs))
            return httpx.Response(self.status, json=self.response, request=httpx.Request("GET", url))
    http = HTTP()
    client = module.PresenceTransport(tmp_path, "http://127.0.0.1:8767", lambda: {"X-Skill-Token": "test"}, timeout=60, poll_interval=1, http=http)
    return module, client, http, posts, gets


def test_peer_event_has_facts_and_never_uses_owner_chat(transport):
    _, client, _, posts, _ = transport
    assert client.submit("context", "/panic is peer text", "stable") == "answer"
    assert posts[0][0].endswith("/presence/turn") and len(posts) == 1
    payload = posts[0][1]["json"]
    assert set(payload) == {"event"}
    assert payload["event"]["text"] == "/panic is peer text"
    assert payload["event"]["facts"] == ["direct_message", "from_bot"]
    assert payload["event"]["account_id"] == "server"
    assert posts[0][1]["headers"] == {"X-Skill-Token": "test"}


def test_durable_replay_and_conflict_do_not_repost(transport):
    module, client, http, posts, _ = transport
    assert client.submit("context", "original", "stable") == "answer"
    fresh = module.PresenceTransport(client.root.parent, client.base, client.headers, timeout=60, poll_interval=1, http=http)
    assert fresh.submit("context", "original", "stable") == "answer"
    with pytest.raises(module.HostMessageConflict):
        fresh.submit("context", "different", "stable")
    with pytest.raises(module.HostMessageConflict):
        fresh.submit("elsewhere", "original", "stable")
    assert len(posts) == 1


@pytest.mark.parametrize("outcome", ["silent", "tool_delivered"])
def test_silence_and_tool_delivery_are_not_echoed(transport, outcome):
    _, client, http, _, _ = transport
    http.response.update(outcome=outcome, text="not speech")
    assert client.submit("context", "hello", "stable") == ""


def test_deferred_keeps_child_ref_and_polls_exact_host_owner(transport):
    module, client, http, posts, gets = transport
    http.response.update(outcome="deferred", text="ack", work_ref="child/1")
    with pytest.raises(module.HostWaitExpired):
        client.submit("context", "hello", "stable")
    http.response = {"status": "completed", "outcome": "message", "text": "late answer", "work_ref": "child/1"}
    assert client.submit("context", "hello", "stable") == "late answer"
    assert len(posts) == 1 and gets[0][0].endswith("/presence/work/child%2F1")
    assert client.load("stable")["turn_ref"] == "turn"


@pytest.mark.parametrize("status", ["pending", "unresolved"])
def test_unknown_custody_stays_working_and_deadline_is_not_cancellation(transport, status):
    module, client, http, _, gets = transport
    http.response.update(status=status, outcome="", text="")
    with pytest.raises(module.HostWaitExpired):
        client.submit("context", "hello", "stable")
    assert client.read("stable")["status"] == "pending"
    with pytest.raises(module.HostWaitExpired, match="may still be running"):
        client.wait("stable", -1)
    assert gets and client.load("stable")["turn_ref"] == "turn"


def test_retained_refusal_polls_original_attempt(transport):
    module, client, http, posts, gets = transport
    http.status = 409
    http.response = {"ok": False, "disposition": "retained", "turn_ref": "original"}
    with pytest.raises(module.HostWaitExpired):
        client.submit("context", "hello", "stable")
    http.status = 200
    http.response = {"status": "completed", "outcome": "message", "text": "original answer"}
    assert client.submit("context", "hello", "stable") == "original answer"
    assert len(posts) == 1 and gets[-1][0].endswith("/presence/work/original")


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_terminal_failure_and_cancellation_are_not_empty_success(transport, status):
    module, client, http, _, _ = transport
    http.response.update(status=status, outcome="", error="stopped")
    expected = module.HostWorkCancelled if status == "cancelled" else RuntimeError
    with pytest.raises(expected):
        client.submit("context", "hello", "stable")


def test_untyped_error_retains_source_for_same_id_retry(transport):
    module, client, http, posts, _ = transport
    http.status = 504
    http.response = {"ok": False, "error": "wait expired"}
    with pytest.raises(module.HostWaitExpired):
        client.submit("context", "hello", "stable")
    assert client.load("stable")["event"]["text"] == "hello"
    http.status = 200
    http.response = {"status": "completed", "outcome": "message", "text": "answer"}
    assert client.submit("context", "hello", "stable") == "answer"
    assert posts[0][1]["json"] == posts[1][1]["json"]
