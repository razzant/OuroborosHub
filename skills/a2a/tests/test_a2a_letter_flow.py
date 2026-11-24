"""Ordinary A2A exchanges with invented letters, executed only by the offline runner."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from skills.a2a import plugin
from skills.a2a.lib import client, letters, links
from skills.a2a.scripts import a2a_watch

PEER = "https://orchard.example.test/letters"
HOST = "http://127.0.0.1:8767"  # only an address label for the in-process host fixture
QUESTION = "  How is the orchard?\n\nPlease include the pears.\n"
ANSWER = "  The pears are ripe.\n\nThe apples need a week.\n"
ORIGIN = {"task_id": "orchard-work", "call_ref": "fixture-call"}


def agent_text(message_id, text, role="agent"):
    return {"kind": "message", "role": role, "messageId": message_id,
            "parts": [{"kind": "text", "text": text}]}


class OrchardPeer:
    def __init__(self, state, *, ready_after=1, clip_once=False, authenticated=False, fast=False, url=PEER):
        self.state, self.ready_after = state, ready_after
        self.clip_once, self.authenticated = clip_once, authenticated
        self.fast, self.url = fast, url
        self.message, self.calls, self.reads = None, [], 0
        self.send_timeout = None
        self.transport = httpx.MockTransport(self.exchange)

    def exchange(self, request):
        assert request.url == httpx.URL(self.url)
        if self.authenticated:
            assert request.headers["authorization"] == "Basic " + base64.b64encode(b"ouroboros:fixture-peer").decode()
        payload = json.loads(request.content)
        self.rpc_id = payload["id"]
        method = payload["method"]
        self.calls.append(method)
        result = {"kind": "task", "id": "orchard-task", "contextId": "orchard-context"}
        if method == "message/send":
            self.message = payload["params"]["message"]
            assert self.message["parts"][0]["text"] == QUESTION
            assert payload["params"]["configuration"] == {"blocking": False}
            stored = letters.load(self.state, self.message["messageId"])
            assert stored["state"] == "sent_no_handle" and stored["dispatch"]["state"] == "uncertain"
            self.send_timeout = request.extensions["timeout"]["read"]
            result["status"] = {"state": "submitted"}
            if self.fast:
                result["status"] = {"state": "completed", "message": agent_text("orchard-answer", ANSWER)}
                result["history"] = [self.message]
        else:
            assert method == "tasks/get" and payload["params"]["id"] == "orchard-task"
            self.reads += 1
            ongoing = self.reads < self.ready_after
            result["status"] = {"state": "working"} if ongoing else {
                "state": "completed", "message": agent_text("orchard-answer", ANSWER)}
            history = [self.message, agent_text("step-one", "Checking the pears"),
                       agent_text("step-two", "Checking the apples")]
            cap = payload["params"].get("historyLength")
            if self.clip_once and self.reads == 1:
                cap = 2  # this peer first returned only its recent progress
            result["history"] = history if cap is None else (history[-cap:] if cap else [])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})


class RoomReceipts:
    def __init__(self, delivery, *, defer_once=False):
        self.delivery, self.defer_once, self.reports = delivery, defer_once, []

    def get(self, url, **kwargs):
        assert url == HOST + "/identity"
        return httpx.Response(200, json={"late_result_version": 1})

    def post(self, url, *, json, **kwargs):
        assert url == HOST + "/late_result"
        assert set(json) == {"call_ref", "report_id", "label", "text"}
        assert json["text"] == ANSWER and json["call_ref"] == "fixture-call"
        self.reports.append(json)
        if self.defer_once and len(self.reports) == 1:
            return httpx.Response(429, json={"retry_after_s": 2})
        return httpx.Response(200, json={"ok": True, "report_ref": "orchard-receipt", "delivery": self.delivery})


def watching(tmp_path, peer, host, wall):
    return a2a_watch.LetterWatch(tmp_path, client=client, letters=letters, host_url=HOST,
                                 headers=dict, http=lambda: host, transport=lambda: peer.transport,
                                 clock=lambda: wall[0])


def test_terminal_history_keeps_normal_slow_reply(tmp_path):
    peer = OrchardPeer(tmp_path, ready_after=3)
    ticks = [0.0]

    def sleep(seconds):
        ticks[0] += seconds

    inline = json.loads(client.send(tmp_path, PEER, QUESTION, asked_by=ORIGIN, transport=peer.transport,
                                    clock=lambda: ticks[0], sleep=sleep))
    message_id = inline["message_id"]
    assert inline["state"] == "completed" and inline["answer"] == ANSWER
    assert inline["dispatch"]["state"] == "accepted"
    assert inline["late_answer"] == {"report_id": message_id, "label": "https://orchard.example.test:443/letters",
                                      "text": ANSWER}
    assert "fixture-call" not in json.dumps(inline)
    stored = letters.load(tmp_path, message_id)
    assert "asker_received_at" not in stored
    assert stored["waiting_until"] == 0
    assert peer.calls == ["message/send", "tasks/get", "tasks/get", "tasks/get"]
    assert ticks[0] == 3 * client.POLL_INTERVAL_SEC
    host = RoomReceipts("tool_result")
    watch = watching(tmp_path, peer, host, [100.0])
    assert watch.scan() == {"read": 0, "reported": 1, "settled": 1}
    saved = json.loads(client.status(tmp_path, message_id=message_id))
    assert saved["answer"] == ANSWER and saved["late_notice"]["delivery"] == "tool_result"


def test_no_wait_releases_reader_and_watcher_hands_off(tmp_path):
    peer = OrchardPeer(tmp_path)

    def no_sleep(_seconds):
        raise AssertionError("wait=False must not poll")

    handle = json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, asked_by=ORIGIN,
                                   transport=peer.transport, sleep=no_sleep))
    assert handle["state"] == "submitted" and handle["late_answer"] is None
    assert peer.calls == ["message/send"]
    assert peer.send_timeout <= client.REQUEST_TIMEOUT_SEC
    host = RoomReceipts("handed_off", defer_once=True)
    wall = [100.0]
    watch = watching(tmp_path, peer, host, wall)
    assert watch.scan() == {"read": 1, "reported": 0, "settled": 0}
    wall[0] += 2
    assert watch.scan() == {"read": 0, "reported": 1, "settled": 1}
    assert host.reports[0] == host.reports[1]
    assert watch.scan() == {"read": 0, "reported": 0, "settled": 0}
    assert peer.calls == ["message/send", "tasks/get"]
    result = json.loads(client.status(tmp_path, message_id=handle["message_id"]))
    assert result["answer"] == ANSWER and result["late_notice"]["delivery"] == "handed_off"


def test_fast_standard_task_returns_its_answer_without_waiting(tmp_path):
    peer = OrchardPeer(tmp_path, fast=True)
    inline = json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, asked_by=ORIGIN, transport=peer.transport))
    assert inline["state"] == "completed" and inline["answer"] == ANSWER
    assert inline["late_answer"] == {"report_id": inline["message_id"],
                                      "label": "https://orchard.example.test:443/letters", "text": ANSWER}
    assert peer.calls == ["message/send"]
    host = RoomReceipts("tool_result")
    watch = watching(tmp_path, peer, host, [100.0])
    assert watch.scan() == {"read": 0, "reported": 1, "settled": 1}
    snapshot = json.loads(client.status(tmp_path, PEER, inline["task_id"], transport=peer.transport))
    assert snapshot["jsonrpc"] == "2.0" and snapshot["id"] == peer.rpc_id
    assert snapshot["result"]["id"] == inline["task_id"]


def test_incomplete_progress_history_keeps_the_task_snapshot_available(tmp_path):
    peer = OrchardPeer(tmp_path, clip_once=True)
    handle = json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, transport=peer.transport))
    args = {"url": PEER, "message_id": handle["message_id"], "transport": peer.transport}
    first = json.loads(client.status(tmp_path, **args))
    assert first["unconfirmed"] and first["state"] == "submitted" and "answer" not in first
    assert f'task_id="{handle["task_id"]}"' in first["note"]
    assert watching(tmp_path, peer, RoomReceipts("handed_off"), [100.0]).scan() == {
        "read": 0, "reported": 0, "settled": 1}
    assert json.loads(client.status(tmp_path, **args))["answer"] == ANSWER
    assert peer.calls == ["message/send", "tasks/get", "tasks/get"]


def test_saved_named_link_keeps_its_binding_for_send_and_status(tmp_path):
    link = links.save(tmp_path, {"id": "orchard", "url": PEER, "send_secret": "fixture-peer"})
    peer = OrchardPeer(tmp_path, authenticated=True)
    saved = json.loads(client.send(tmp_path, "orchard", QUESTION, budget=0, transport=peer.transport))
    assert saved["state"] == "saved" and not peer.calls
    handle = json.loads(client.send(tmp_path, "orchard", QUESTION, message_id=saved["message_id"], wait=False,
                                    transport=peer.transport))
    relabeled = links.save(tmp_path, {"id": "orchard", "label": "Orchard keeper"})
    assert link.uid == relabeled.uid
    assert json.loads(client.status(tmp_path, "orchard", message_id=handle["message_id"],
                                    transport=peer.transport))["answer"] == ANSWER
    assert peer.calls == ["message/send", "tasks/get"]


def test_a_manual_environment_pair_keeps_its_exact_endpoint(tmp_path):
    actual = PEER + "/"  # the 1.5.3 client posted to this subpath with its final slash
    peer = OrchardPeer(tmp_path, authenticated=True, url=actual)
    with patch.dict(os.environ, {"A2A_CLIENT_PEER_URL": actual, "A2A_CLIENT_PASSWORD": "fixture-peer"}):
        handle = json.loads(client.send(tmp_path, actual, QUESTION, wait=False, transport=peer.transport))
        stored = letters.load(tmp_path, handle["message_id"])
        assert stored["link_uid"] == "legacy:client-env"
        assert "fixture-peer" not in json.dumps(stored) and not (tmp_path / links.LINKS_FILE).exists()
        assert json.loads(client.status(tmp_path, actual, message_id=handle["message_id"],
                                        transport=peer.transport))["answer"] == ANSWER
    assert peer.calls == ["message/send", "tasks/get"]


def test_a_direct_agent_message_keeps_its_original_call(tmp_path):
    calls = []

    def exchange(request):
        payload = json.loads(request.content)
        assert payload["method"] == "message/send"
        message = payload["params"]["message"]
        calls.append(message["messageId"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                                         "result": agent_text("direct-orchard-answer", ANSWER)})

    inline = json.loads(client.send(tmp_path, PEER, QUESTION, asked_by=ORIGIN,
                                    transport=httpx.MockTransport(exchange)))
    assert calls == [inline["message_id"]]
    assert inline["state"] == "completed" and inline["task_id"] == ""
    assert inline["late_answer"]["report_id"] == calls[0] and inline["late_answer"]["text"] == ANSWER
    host = RoomReceipts("tool_result")
    watch = a2a_watch.LetterWatch(tmp_path, client=client, letters=letters, host_url=HOST, headers=dict,
                                 http=lambda: host, clock=lambda: 100.0)
    assert watch.scan() == {"read": 0, "reported": 1, "settled": 1}


def test_two_letters_on_the_same_task_read_their_own_answers(tmp_path):
    ids, reads = [], []
    first_answer = "The apples still need a week."

    def exchange(request):
        call = json.loads(request.content)
        result = {"kind": "task", "id": "orchard-shared", "contextId": "orchard-summer"}
        if call["method"] == "message/send":
            message = call["params"]["message"]
            ids.append(message["messageId"])
            if len(ids) == 2:
                assert message["taskId"] == "orchard-shared" and message["contextId"] == "orchard-summer"
            state, delivery = ("working", "working") if len(ids) == 1 else ("submitted", "saved")
            result["status"] = {"state": state}
            result["metadata"] = {"ouroboros": {"message_id": ids[-1], "pending_message_ids": ids.copy(),
                                                 "delivery": {"class": delivery}}}
        else:
            assert call["method"] == "tasks/get" and call["params"]["id"] == "orchard-shared"
            reads.append(call["id"])
            result["status"] = {"state": "completed"}
            outcome = {"message_id": ids[1], "answers": {ids[1]: {"state": "completed", "answer": ANSWER}},
                       "pending_message_ids": [ids[0]] if len(reads) == 1 else []}
            if len(reads) > 1:
                outcome["answers"][ids[0]] = {"state": "completed", "answer": first_answer}
            result["metadata"] = {"ouroboros": outcome}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": result})

    wire = httpx.MockTransport(exchange)
    first = json.loads(client.send(tmp_path, PEER, "Are the apples ready?", wait=False, transport=wire))
    second = json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, task_id=first["task_id"],
                                    context_id=first["context_id"], transport=wire))
    assert (first["state"], first["delivery"]["class"]) == ("working", "working")
    assert (second["state"], second["delivery"]["class"]) == ("submitted", "saved")
    pending = json.loads(client.status(tmp_path, PEER, message_id=first["message_id"], transport=wire))
    assert pending["state"] == "submitted" and "answer" not in pending
    assert not letters.load(tmp_path, first["message_id"]).get("watch_ended")
    assert json.loads(client.status(tmp_path, PEER, message_id=first["message_id"], transport=wire))["answer"] == first_answer
    assert json.loads(client.status(tmp_path, PEER, message_id=second["message_id"], transport=wire))["answer"] == ANSWER
    assert ids == [first["message_id"], second["message_id"]]


def test_standard_continuation_keeps_its_own_reply(tmp_path):
    inputs = []
    first_question = agent_text("orchard-clarifier", "Which fruit first?")
    second_question = agent_text("orchard-follow-up", "Do you need the harvest date too?")

    def exchange(request):
        payload = json.loads(request.content)
        assert request.url == httpx.URL(PEER)
        history = [inputs[0], first_question, inputs[1]] if len(inputs) > 1 else []
        result = {"kind": "task", "id": "orchard-task", "contextId": "orchard-context"}
        if payload["method"] == "message/send":
            message = payload["params"]["message"]
            if inputs:
                assert message["taskId"] == "orchard-task" and message["contextId"] == "orchard-context"
            inputs.append(message)
            if len(inputs) == 1:
                result.update(history=inputs.copy(), status={"state": "input-required", "message": first_question})
            elif len(inputs) == 2:
                result["status"] = {"state": "working"}
            else:
                result.update(history=[*history, second_question, message],
                              status={"state": "completed", "message": agent_text("orchard-reply", ANSWER)})
        else:
            assert payload["method"] == "tasks/get" and len(inputs) == 2
            result.update(history=history, status={"state": "input-required", "message": second_question})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    wire = httpx.MockTransport(exchange)
    first = json.loads(client.send(tmp_path, PEER, "Tell me about the orchard.", wait=False, transport=wire))
    assert (first["state"], first["answer"]) == ("input-required", "Which fruit first?")
    conversation = {"task_id": first["task_id"], "context_id": first["context_id"], "transport": wire}
    second = json.loads(client.send(tmp_path, PEER, "The pears, please.", wait=False, **conversation))
    assert second["state"] == "working"
    clarified = json.loads(client.status(tmp_path, PEER, message_id=second["message_id"], transport=wire))
    assert (clarified["state"], clarified["answer"]) == ("input-required", "Do you need the harvest date too?")
    final = json.loads(client.send(tmp_path, PEER, "Yes, please.", wait=False, asked_by=ORIGIN, **conversation))
    assert (final["state"], final["answer"]) == ("completed", ANSWER)
    assert final["late_answer"]["report_id"] == inputs[2]["messageId"]
    for question in (first, clarified):
        reread = json.loads(client.status(tmp_path, PEER, message_id=question["message_id"], transport=wire))
        assert (reread["state"], reread["answer"]) == ("input-required", question["answer"])


def test_overlapping_standard_letters_keep_an_unassigned_answer_unconfirmed(tmp_path):
    inputs = []
    question = agent_text("orchard-question", "What would you like to know?")

    def exchange(request):
        call = json.loads(request.content)
        result = {"kind": "task", "id": "shared-plan", "contextId": "shared-orchard"}
        if call["method"] == "message/send":
            inputs.append(call["params"]["message"])
        if len(inputs) == 1:
            result.update(history=inputs.copy(), status={"state": "input-required", "message": question})
        elif len(inputs) == 2:
            result.update(status={"state": "working"})
        else:
            # The shared task shows its last input and an overall result, not which input it answers.
            result.update(history=[inputs[0], question, inputs[-1]],
                          status={"state": "completed", "message": agent_text("orchard-report", ANSWER)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": result})

    transport = httpx.MockTransport(exchange)
    initial = json.loads(client.send(tmp_path, PEER, "Can you help with the orchard?", wait=False, transport=transport))
    args = {"task_id": initial["task_id"], "context_id": initial["context_id"], "transport": transport, "wait": False}
    pears = json.loads(client.send(tmp_path, PEER, "How many pears?", **args))
    apples = json.loads(client.send(tmp_path, PEER, "And the apple delivery?", **args))
    assert apples["unconfirmed"] and "answer" not in apples
    pears_read = json.loads(client.status(tmp_path, PEER, message_id=pears["message_id"], transport=transport))
    assert pears_read["unconfirmed"] and "answer" not in pears_read


def test_legacy_manual_receiver_keeps_its_exact_letter_receipt(tmp_path):
    import hashlib

    calls = []

    def exchange(request):
        call = json.loads(request.content)
        calls.append(call["method"])
        message_id = call["params"]["message"]["messageId"]
        legacy_digest = hashlib.sha256(f'["orchard-task","{message_id}"]'.encode()).hexdigest()
        result = {"id": "orchard-task", "contextId": "orchard-context", "status": {"state": "completed"},
                  "ouroboros": {"client_message_id": "a2a:" + legacy_digest},
                  "artifacts": [{"parts": [{"kind": "text", "text": ANSWER}]}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": result})

    transport = httpx.MockTransport(exchange)
    handle = json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, transport=transport, asked_by=ORIGIN))
    assert calls == ["message/send"]
    assert (handle["state"], handle["answer"], handle["task_id"]) == ("completed", ANSWER, "orchard-task")
    assert handle["late_answer"]["report_id"] == handle["message_id"]

    def reply_later_than_client(request):
        calls.append(json.loads(request.content)["method"])
        raise httpx.ReadTimeout("this ordinary reply exceeded the client's wait", request=request)

    old = httpx.MockTransport(reply_later_than_client)
    unknown = json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, transport=old))
    assert unknown["state"] == "sent_no_handle" and unknown["task_id"] == "" and "answer" not in unknown
    args = {"message_id": unknown["message_id"], "transport": old}
    assert json.loads(client.status(tmp_path, PEER, **args))["state"] == "sent_no_handle"
    assert json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, **args))["state"] == "sent_no_handle"
    assert calls == ["message/send", "message/send"]  # two independent letters; no poll or repeated send


def test_task_snapshot_preserves_the_peer_rpc_error(tmp_path):
    calls = []
    error = {"code": -32001, "message": "Orchard plan is not available", "data": {"resource": "orchard-plan"}}

    for http_status in (200, 503):
        def exchange(request):
            call = json.loads(request.content)
            assert call["method"] == "tasks/get" and call["params"] == {"id": "orchard-plan"}
            calls.append(call["id"])
            return httpx.Response(http_status, json={"jsonrpc": "2.0", "id": call["id"], "error": error})

        result = json.loads(client.status(tmp_path, PEER, "orchard-plan", transport=httpx.MockTransport(exchange)))
        assert result == {"jsonrpc": "2.0", "id": calls[-1], "error": error}
    assert len(calls) == 2 and calls[0] != calls[1]


def test_status_listing_keeps_only_the_latest_scoped_letters(tmp_path):
    for number in range(72):
        client.send(tmp_path, PEER, f"Harvest note {number}", message_id=f"note-{number}", budget=0,
                    asked_by={"task_id": "orchard" if number < 64 else "gate"})
    with patch.object(letters.storage, "read_json", wraps=letters.storage.read_json) as read:
        assert json.loads(client.status(tmp_path))["letters"] == [] and read.call_count == 0
        assert json.loads(client.status(tmp_path, message_id="note-0"))["state"] == "saved"
        assert read.call_count == 1
        read.reset_mock()
        report = json.loads(client.status(tmp_path, asked_by={"task_id": "orchard"}))["letters"]
        assert read.call_count == 72 and len(report) == 50
        assert all(int(row["message_id"].removeprefix("note-")) < 64 for row in report)


def test_unaccepted_follow_up_does_not_change_the_initial_answer(tmp_path):
    peer = OrchardPeer(tmp_path)
    first = json.loads(client.send(tmp_path, PEER, QUESTION, wait=False, transport=peer.transport))

    def unavailable(request):
        payload = json.loads(request.content)
        assert payload["method"] == "message/send" and payload["params"]["message"]["taskId"] == first["task_id"]
        raise httpx.ConnectError("fixture has not connected", request=request)

    second = json.loads(client.send(tmp_path, PEER, "And next week's harvest?", wait=False, task_id=first["task_id"],
                                     context_id=first["context_id"], transport=httpx.MockTransport(unavailable)))
    assert second["dispatch"]["state"] == "unsent" and second["task_id"] == ""
    read = json.loads(client.status(tmp_path, PEER, message_id=first["message_id"], transport=peer.transport))
    assert read["state"] == "completed" and read["answer"] == ANSWER
    assert peer.calls == ["message/send", "tasks/get"]


def test_named_discovery_reads_the_documented_card(tmp_path):
    links.save(tmp_path, {"id": "orchard", "url": PEER, "send_secret": "fixture-peer"})

    def exchange(request):
        assert request.method == "GET"
        assert request.url == httpx.URL(PEER + "/.well-known/agent-card.json")
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"ouroboros:fixture-peer").decode()
        return httpx.Response(200, json={"name": "Orchard keeper", "url": PEER, "security": [{"basic": []}]})

    card = json.loads(client.discover(tmp_path, "orchard", transport=httpx.MockTransport(exchange)))
    assert card["name"] == "Orchard keeper" and card["security"] == [{"basic": []}]


def load_receiver(state, **settings):
    """Import the real SDK-built ASGI app, before giving it an in-process Host Service."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "a2a_daemon.py"
    spec = importlib.util.spec_from_file_location("_letter_receiver_fixture", script)
    daemon = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"OUROBOROS_SKILL_STATE_DIR": str(state), "A2A_TOOLS_REFRESHER": "0", **settings}):
        spec.loader.exec_module(daemon)
    return daemon


class OrchardHost:
    def __init__(self, *, work_ref="orchard-child", result_status="completed"):
        self.work_ref, self.ready, self.events = work_ref, False, []
        self.result_status = result_status

    def get(self, url, **kwargs):
        if url == HOST + "/identity":
            return httpx.Response(200, json={"name": "Fixture host", "places_version": 1, "presence_identity": 1})
        if url == HOST + "/tools/schemas":
            return httpx.Response(200, json={"tools": [{"name": "read_orchard", "description": "Read the orchard"}]})
        if url.startswith(HOST + "/presence/place?"):
            return httpx.Response(200, json={"place_id": "orchard-place"})
        assert self.work_ref and url.startswith(HOST + "/presence/work/" + self.work_ref)
        if not self.ready:
            return httpx.Response(202, json={"ok": True, "status": "pending", "work_ref": self.work_ref})
        return httpx.Response(200, json={"ok": True, "status": self.result_status, "work_ref": self.work_ref,
                                         "outcome": "message", "text": ANSWER, "output_ref": "orchard-output"})

    def post(self, url, *, json, **kwargs):
        assert url == HOST + "/presence/turn"
        assert json["binding_id"] == "orchard-place"
        self.events.append(json["event"])
        return httpx.Response(200, json={"ok": True, "status": "pending", "work_ref": self.work_ref})


async def rpc(http, method, params):
    reply = await http.post("/", json={"jsonrpc": "2.0", "id": method, "method": method, "params": params})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data.get("id") == method and "result" in data, data
    return data["result"]


def test_sdk_receiver_keeps_bound_answer_after_initial_wait(tmp_path):
    daemon = load_receiver(tmp_path)
    fixture_secret = "orchard-inbound-fixture"
    link = links.save(tmp_path, {"id": "orchard", "label": "Orchard keeper", "accept_secret": fixture_secret})
    host = OrchardHost()
    daemon._HOST_HTTP = host
    daemon._RECORDS.window = 1.0  # shorten the ordinary first wait; its retained work remains addressable
    daemon.A2A_PROGRESS_POLL_SEC = 0.01

    async def exchange():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.app), base_url="http://fixture.test",
                                     auth=("orchard", fixture_secret)) as http:
            first = await rpc(http, "message/send", {"message": agent_text("orchard-first", QUESTION, role="user"),
                                                     "configuration": {"blocking": True}})
            assert first["status"]["state"] == "working"
            assert len(host.events) == 1
            assert host.events[0]["text"] == QUESTION
            actor = host.events[0]["actor"]
            assert (actor["display_name"], actor["origin"], actor["platform_actor_id"]) == (
                "Orchard keeper", "machine", link.principal)
            assert actor["is_bot"] is True and actor["authenticated"] is True
            assert host.events[0]["conversation_id"] == link.principal
            assert len(daemon._RECORDS.pending_arrivals()) == 1
            host.ready = True
            params = {"id": first["id"]}
            result = await rpc(http, "tasks/get", params)
            assert result["status"]["state"] == "completed"
            assert result["metadata"]["ouroboros"]["answers"]["orchard-first"]["answer"] == ANSWER
            assert result["artifacts"][0]["parts"][0]["text"] == ANSWER
            assert not daemon._RECORDS.pending_arrivals()
            reread = await rpc(http, "tasks/get", params)
            assert reread["metadata"]["ouroboros"]["answers"] == result["metadata"]["ouroboros"]["answers"]
            assert len(host.events) == 1

    asyncio.run(exchange())


def test_sdk_expired_receipt_without_active_work_is_not_working(tmp_path):
    daemon = load_receiver(tmp_path)
    host = OrchardHost(work_ref="")
    daemon._HOST_HTTP = host

    async def exchange():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.app), base_url="http://fixture.test") as http:
            first = await rpc(http, "message/send", {"message": agent_text("orchard-receipt", QUESTION, role="user"),
                                                     "configuration": {"blocking": True}})
            assert first["status"]["state"] == "working"
            assert host.events[0]["text"] == QUESTION
            receipt = daemon._presence_transport().load(host.events[0]["source_event_id"])
            daemon._presence_transport().clock = lambda: receipt["deadline"] + 1
            result = await rpc(http, "tasks/get", {"id": first["id"]})
            assert result["status"]["state"] == "completed"
            assert result["metadata"]["ouroboros"]["delivery"]["class"] == "unknown"
            assert len(host.events) == 1

    asyncio.run(exchange())


def test_each_pending_letter_has_its_own_delivery_window(tmp_path):
    daemon = load_receiver(tmp_path)
    host = OrchardHost()
    daemon._HOST_HTTP = host
    records, transport = daemon._RECORDS, daemon._presence_transport()
    actor = daemon._actor("")
    records.window = 1
    old, _ = records.reserve("", "orchard-earlier", {}, "Is the gate open?", actor=actor)
    records.window = 60
    new, _ = records.reserve("", "orchard-later", {}, QUESTION, actor=actor, task_id=old["task_id"],
                             context_id=old["context_id"])
    transport.clock = lambda: old["deadline"] + 1
    daemon._IN_FLIGHT.add(new["digest"])  # the second ordinary executor already owns its request

    observed = asyncio.run(daemon._observe_task_status(old["task_id"], ""))

    assert observed["answers"]["orchard-earlier"]["state"] == "rejected"
    assert observed["answers"]["orchard-earlier"]["delivery"]["class"] == "not_taken_up"
    assert observed["status"]["state"] == "working" and "orchard-later" not in observed["answers"]
    assert records.open_messages(observed) == [(new["digest"], "orchard-later")]
    assert not host.events


def test_late_host_reference_keeps_the_earlier_letter_readable(tmp_path):
    daemon = load_receiver(tmp_path)
    records = daemon._RECORDS
    actor = daemon._actor("")
    old, _ = records.reserve("", "orchard-earlier", {}, QUESTION, actor=actor)
    wall = [old["created_at"]]
    during_post = []

    class TakingUpHost(OrchardHost):
        def post(self, url, *, json, **kwargs):
            # While accepting an ordinary first letter, the host takes longer than its local window.
            wall[0] = old["deadline"] + 1
            during_post.append(transport.read(old["digest"])["status"])
            return super().post(url, json=json, **kwargs)

    host = TakingUpHost()
    daemon._HOST_HTTP = host
    transport = daemon._presence_transport()
    transport.clock = lambda: wall[0]
    pending = transport.offer(old["event"], old["digest"], deadline=old["deadline"])
    new, _ = records.reserve("", "orchard-later", {}, "And the apples?", actor=actor, task_id=old["task_id"],
                             context_id=old["context_id"])
    daemon._IN_FLIGHT.add(new["digest"])
    assert during_post == ["pending"] and pending["status"] == "pending"
    host.ready = True
    observed = asyncio.run(daemon._observe_task_status(old["task_id"], ""))
    assert observed["answers"]["orchard-earlier"]["answer"] == ANSWER
    assert observed["status"]["state"] == "working" and "orchard-later" not in observed["answers"]
    assert [event["text"] for event in host.events] == [QUESTION]


def test_legacy_pending_child_keeps_its_own_answer(tmp_path):
    daemon = load_receiver(tmp_path)
    host = OrchardHost(result_status="failed")
    host.ready = True
    daemon._HOST_HTTP = host
    transport = daemon._presence_transport()
    source = daemon._client_message_id("old-orchard", "orchard-legacy")
    # A normal 1.5 receipt: the child was assigned, but the last durable view was still pending.
    transport._save(source, {"event": {"source_event_id": source}, "turn_ref": "old-turn",
                             "work_ref": host.work_ref, "response": {"ok": True, "status": "pending",
                                                                     "work_ref": host.work_ref}})
    result = transport.read(source)
    assert result["status"] == "failed" and result["text"] == ANSWER
    assert daemon._public(result)[:2] == ("failed", "answered")


def test_reservation_restart_finishes_its_own_letter(tmp_path):
    daemon = load_receiver(tmp_path)

    class ReadyHost(OrchardHost):
        def post(self, url, *, json, **kwargs):
            assert url == HOST + "/presence/turn" and json["binding_id"] == "orchard-place"
            self.events.append(json["event"])
            return httpx.Response(200, json={"ok": True, "status": "completed", "work_ref": "date-letter",
                                             "outcome": "message", "output_ref": "date-reply", "text": ANSWER})

    host = ReadyHost(work_ref=None)  # its ordinary identity/place reads are allowed; no work GET is needed
    daemon._HOST_HTTP = host
    records = daemon._RECORDS
    actor = daemon._actor("")
    first, _ = records.reserve("", "orchard-initial", {}, "Which orchard?", actor=actor)
    task_id, context_id = first["task_id"], first["context_id"]
    with patch.object(records, "attach_arrival", side_effect=OSError("fixture stopped between writes")):
        with pytest.raises(OSError, match="between writes"):
            records.reserve("", "orchard-date", {}, QUESTION, actor=actor, task_id=task_id, context_id=context_id)
    second = records.load_arrival("", "orchard-date")
    assert second["event"]["text"] == QUESTION and second["deadline"] == second["created_at"] + records.window
    assert records.bound(task_id)["client_message_id"] == first["digest"]
    daemon._settle(task_id, "", "orchard-initial", first["digest"], "completed", "answered", "The north orchard.")

    restarted = load_receiver(tmp_path)
    restarted._HOST_HTTP = host

    async def resume():
        await restarted._resume_custody()
        await asyncio.gather(*restarted._BACKGROUND)
        await restarted._resume_custody()  # already settled: the same restart cannot send a second offer

    asyncio.run(resume())
    record = restarted._RECORDS.load_task(task_id)
    assert record["answers"]["orchard-initial"]["answer"] == "The north orchard."
    assert record["answers"]["orchard-date"]["answer"] == ANSWER
    assert restarted._RECORDS.load_arrival("", "orchard-date") == second
    assert host.events == [second["event"]]
    assert not restarted._RECORDS.pending_arrivals()


def test_sdk_blocking_and_streaming_repeats_follow_the_older_letter(tmp_path):
    from a2a.types import Message, Part, Role

    for method in ("message/send", "message/stream"):
        daemon = load_receiver(tmp_path / method.split("/")[-1])
        daemon.A2A_PROGRESS_POLL_SEC = 0.01
        first_text = "Is the gate open?"
        first_reply = "Yes, the gate is open."

        class TwoLetters(OrchardHost):
            def __init__(self):
                super().__init__(work_ref="gate-letter")
                self.reads = 0

            def post(self, url, *, json, **kwargs):
                assert url == HOST + "/presence/turn"
                event = json["event"]
                self.events.append(event)
                if event["text"] == first_text:
                    return httpx.Response(200, json={"ok": True, "status": "pending", "work_ref": self.work_ref})
                return httpx.Response(200, json={"ok": True, "status": "completed", "work_ref": "pear-letter",
                                                 "outcome": "message", "output_ref": "pear-output", "text": ANSWER})

            def get(self, url, **kwargs):
                if url.startswith(HOST + "/presence/work/"):
                    assert url.startswith(HOST + "/presence/work/" + self.work_ref)
                    self.reads += 1
                    if self.ready:
                        return httpx.Response(200, json={"ok": True, "status": "completed", "work_ref": self.work_ref,
                                                          "outcome": "message", "output_ref": "gate-output",
                                                          "text": first_reply})
                return super().get(url, **kwargs)

        host = TwoLetters()
        daemon._HOST_HTTP = host
        transport = daemon._presence_transport()
        records = daemon._RECORDS
        actor = daemon._actor("")

        def reserve(wire, text, **where):
            message = Message(message_id=wire, role=Role.ROLE_USER, parts=[Part(text=text)])
            arrival, _ = records.reserve("", wire, daemon._content(message), text, actor=actor, **where)
            view = transport.offer(arrival["event"], arrival["digest"], deadline=arrival["deadline"])
            daemon._settle_view(arrival["task_id"], "", wire, arrival["digest"], view)
            return arrival

        first = reserve("orchard-gate", first_text)
        task_id, context_id = first["task_id"], first["context_id"]
        reserve("orchard-pears", QUESTION, task_id=task_id, context_id=context_id)

        async def replay():
            message = agent_text("orchard-gate", first_text, role="user")
            message.update(taskId=task_id, contextId=context_id)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.app),
                                         base_url="http://fixture.test") as http:
                before = host.reads
                call = {"jsonrpc": "2.0", "id": "orchard-repeat", "method": method,
                        "params": {"message": message, "configuration": {"blocking": True}}}
                pending = asyncio.create_task(http.post("/", json=call))
                for _ in range(200):
                    if pending.done() or host.reads > before + 1:
                        break
                    await asyncio.sleep(0.01)
                assert not pending.done() and host.reads > before + 1
                host.ready = True
                response = await asyncio.wait_for(pending, 2)
                assert response.status_code == 200, response.text
                frames = ([response.json()] if method == "message/send" else
                          [json.loads(line.split(":", 1)[1]) for line in response.text.splitlines()
                           if line.startswith("data:")])
                assert frames and frames[-1].get("id") == "orchard-repeat", response.text
                old = frames[-1]["result"]
                assert old["metadata"]["ouroboros"]["answers"]["orchard-gate"]["answer"] == first_reply
                assert all(ANSWER.strip() not in json.dumps(frame) for frame in frames)
                current = await rpc(http, "tasks/get", {"id": task_id})
                assert current["metadata"]["ouroboros"]["message_id"] == "orchard-pears"
                assert current["artifacts"][0]["parts"][0]["text"] == ANSWER

        asyncio.run(replay())
        assert [event["text"] for event in host.events] == [first_text, QUESTION]


def test_public_card_and_settings_describe_the_configured_auth(tmp_path):
    url = "https://orchard.example.test/a2a"
    daemon = load_receiver(tmp_path, A2A_PUBLIC_URL=url, A2A_SERVER_PASSWORD="fixture-inbound")
    daemon._HOST_HTTP = OrchardHost()

    async def card():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.app), base_url="http://fixture.test",
                                    auth=("orchard", "fixture-inbound")) as http:
            reply = await http.get("/.well-known/agent-card.json")
            assert reply.status_code == 200
            assert reply.json()["security"] == [{"basic": []}]
            assert reply.json()["url"] == url

    asyncio.run(card())
    assert plugin._configured_endpoint({"A2A_PUBLIC_URL": url}) == (url, True)
    assert plugin._configured_endpoint({}) == ("http://127.0.0.1:18800/", False)


def test_form_and_start_keep_an_explicit_public_address(tmp_path):
    class SettingsAPI:
        def get_state_dir(self):
            return tmp_path

    class Form:
        def __init__(self, url):
            self.url = url

        async def json(self):
            return {"A2A_PUBLIC_URL": self.url}

    save = plugin._make_settings_save(SettingsAPI())
    url = PEER + "/"
    assert asyncio.run(save(Form(url))).status_code == 200
    assert asyncio.run(save(Form("orchard.example.test"))).status_code == 400
    assert links.load_server_settings(tmp_path)["A2A_PUBLIC_URL"] == url
    assert load_receiver(tmp_path, A2A_SERVER_PASSWORD="orchard-inbound-fixture")._card_url() == url
    with pytest.raises(ValueError, match="A2A_PUBLIC_URL"):
        load_receiver(tmp_path, A2A_PUBLIC_URL="orchard.example.test")
    assert asyncio.run(save(Form(""))).status_code == 200
    assert load_receiver(tmp_path)._card_url() == "http://127.0.0.1:18800/"


def test_watcher_main_enters_normal_scan_loop(tmp_path):
    observations = []
    scan = a2a_watch.LetterWatch.scan

    def observe(instance):
        observations.append(scan(instance))
        return observations[-1]

    class EndOfFixture(Exception):
        pass

    def slept(seconds):
        assert seconds == a2a_watch.SCAN_SEC
        raise EndOfFixture

    with patch.dict(os.environ, {"OUROBOROS_SKILL_STATE_DIR": str(tmp_path)}), \
            patch.object(a2a_watch.LetterWatch, "scan", observe):
        try:
            a2a_watch.main(sleep=slept)
        except EndOfFixture:
            pass
    assert observations == [{"read": 0, "reported": 0, "settled": 0}]
