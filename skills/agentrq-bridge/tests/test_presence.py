import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("agentrq_presence", Path(__file__).resolve().parents[1] / "presence.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_provider_facts_and_identity_preserve_full_notification():
    params = {"type": "message.created", "taskId": "task", "sender": "human", "text": "/panic " + "x" * 600}
    event = module.provider_event(params, "https://user:secret@workspace.example/mcp?token=secret")
    assert event["account_id"] == "https://workspace.example/mcp"
    assert event["facts"] == ["any_message", "direct_message"]
    assert event["text"] == params["text"] and event["message"] == params
    assert module.provider_event(params, "https://workspace.example/mcp")["source_event_id"] == event["source_event_id"]
    different = module.provider_event({**params, "text": params["text"] + "z"}, "https://workspace.example/mcp")
    assert different["source_event_id"] != event["source_event_id"]
    assert module.provider_event({"type": "status.updated", "taskId": "task"}, "https://workspace.example")["facts"] == []
    assert module.provider_event({"type": "task.created", "assignee": "agent"}, "https://workspace.example")["facts"] == ["direct_message"]


@pytest.mark.parametrize("state", ["silent", "paused"])
def test_held_reply_survives_restart_and_active_delivers_once(tmp_path, state):
    sent, calls = [], []
    event = module.provider_event({"type": "message.created", "taskId": "task", "sender": "human", "text": "hello"}, "https://workspace.example")
    inbox = module.PresenceInbox(tmp_path, "http://127.0.0.1:8767", "token", lambda task, text: sent.append((task, text)) or {"ok": True})
    path = inbox.enqueue(event)
    assert inbox.enqueue(event) == path
    place_state = [state]
    def request(method, route, payload=None):
        calls.append((method, route, payload))
        if route.startswith("/presence/place?"):
            return {"state": place_state[0], "label": "AgentRQ"}
        if route == "/identity":  # a Host without continuation version 2: the request bytes stay
            return {"ok": True, "presence_continuation_version": 1}
        assert route == "/presence/turn" and payload == {"event": event}
        return {"status": "completed", "outcome": "message", "text": "answer", "turn_ref": "turn"}
    inbox._request = request
    inbox.process(path)
    held = json.loads(path.read_text())
    assert held["state"] == "held" and held["error"].startswith("Held:") and not sent
    fresh = module.PresenceInbox(tmp_path, inbox.base, "token", inbox.reply)
    fresh._request = request
    place_state[0] = "active"
    fresh.process(path)
    fresh.process(path)
    assert sent == [("task", "answer")]
    assert len([call for call in calls if call[0] == "POST"]) == 1
    assert json.loads(path.read_text())["state"] == "completed"


def test_retained_and_deferred_work_keep_original_references(tmp_path):
    inbox = module.PresenceInbox(tmp_path, "http://127.0.0.1:8767", "token", lambda *args: pytest.fail("silent outcome"))
    path = inbox.enqueue(module.provider_event({"type": "task.created", "taskId": "task", "assignee": "agent"}, "https://workspace.example"))
    replies = iter([
        {"ok": False, "disposition": "retained", "turn_ref": "turn/1"},
        {"status": "unresolved", "turn_ref": "turn/1", "child_work_ref": "child/2"},
        {"status": "completed", "outcome": "silent"},
        {"status": "completed", "outcome": "silent"},
    ])
    routes = []
    def request(method, route, payload=None):
        if route == "/identity":
            return {"ok": True, "presence_continuation_version": 1}
        routes.append(route)
        return next(replies)
    inbox._request = request
    for _ in range(3):
        inbox.process(path)
    # The original turn keeps being read beside the child it named, each until its own end.
    assert routes == ["/presence/turn", "/presence/work/turn%2F1", "/presence/work/turn%2F1", "/presence/work/child%2F2"]
    assert json.loads(path.read_text())["state"] == "completed"


def test_ambiguous_reply_is_not_replayed(tmp_path):
    sent = []
    def reply(*args):
        sent.append(args)
        raise TimeoutError("response lost")
    inbox = module.PresenceInbox(tmp_path, "http://127.0.0.1:8767", "token", reply)
    path = inbox.enqueue(module.provider_event({"taskId": "task", "text": "hello"}, "https://workspace.example"))
    inbox._request = lambda method, route, payload=None: {"state": "active"} if route.startswith("/presence/place?") else {
        "ok": True} if route == "/identity" else {"status": "completed", "outcome": "message", "text": "answer"}
    inbox.process(path)
    inbox.process(path)
    assert len(sent) == 1 and json.loads(path.read_text())["state"] == "uncertain"
