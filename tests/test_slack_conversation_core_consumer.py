"""Slack conversation identity through a real core's Presence routes (local opt-in).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_slack_conversation_core_consumer.py

Without that variable this is reported as skipped, never as passed. The bridge runs unchanged (parser, store,
intake, observation and outbound workers, Slack client, Host client); Slack is scripted and core's own app
answers over ASGI for a connected workspace Place whose chronicle is active, as on a running install. Only
skill-token authentication, the focus allowance and the model are stubbed: core's ``run_presence_turn`` builds
and runs each turn, and the model double opens intake as the agent loop does, then reads the room core renders
for the task core built. Every run is a subprocess whose HOME, data and settings are temporary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"
TOKEN = "fixture-skill-token"
PROACTIVE = "Hi, I am Ouroboros. Who owns the Vault setup for the remote Linux hosts?"
POST = "Vault access for the remote Linux hosts: a plan, no change to infrastructure yet."
FIRST, SECOND = "I do, ask away.", "And the old .env files must keep working."


def _run(tmp_path, scenario):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; the core Presence routes were not exercised")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": os.pathsep.join([str(Path(core).resolve()), str(ROOT / "skills" / "slack-bridge")]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run([sys.executable, __file__, str(tmp_path), scenario], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def _open_conversation(room):
    """A rendered room's header and its verbatim lane (people and my replies), before any task facts."""
    header, _, lane = room.partition("### Open conversation")
    return header, lane.split("\n### ", 1)[0]


def test_a_top_level_answer_joins_the_dm_room_of_the_proactive_message_it_answers(tmp_path):
    facts = _run(tmp_path, "answer")
    room = facts["rooms"]["slack:T1:D1:0"]
    # Ouroboros's own top-level DM, the person's top-level answer and the plain reply live in one room.
    assert [row[:3] for row in facts["rows"]] == [["out", room, PROACTIVE], ["in", room, FIRST],
                                                 ["out", room, "Answered Ev-1"]]
    [turn] = facts["turns"]
    assert turn["source"] == "Ev-1" and turn["chat_id"] == room
    # The answering turn's own room view holds Ouroboros's earlier words verbatim, as its own, in lane 1.
    header, lane = _open_conversation(turn["room"])
    assert "people 1, mine 1, task facts 0" in header
    assert lane.index(PROACTIVE) < lane.index(FIRST)
    assert "Ouroboros [" in next(line for line in lane.splitlines() if PROACTIVE in line)
    assert facts["posts"] == [["D1", "", PROACTIVE], ["D1", "", "Answered Ev-1"]]
    assert facts["statuses"] == {"Ev-1": 200} and facts["inbox"] == {"Ev-1": "delivered"}


def test_a_second_top_level_dm_message_reaches_the_live_author_of_the_first(tmp_path):
    facts = _run(tmp_path, "burst")
    room = facts["rooms"]["slack:T1:D1:0"]
    # Core accepted the first event's queue snapshot under the key it derives itself, listing the second event.
    assert facts["statuses"] == {"Ev-1": 200, "Ev-2": 200}
    assert facts["queue"] == {"conversation_key": "slack:T1:D1:0", "events": ["Ev-2"]}
    # One author: the second message was handed to the first one's live turn, which answered both at once.
    [turn] = facts["turns"]
    assert turn["source"] == "Ev-1" and turn["heard"] == ["Ev-2"]
    attention = {key: facts["attention"]["Ev-2"].get(key) for key in ("decision", "reason", "recipient")}
    assert attention == {"decision": "heard", "reason": "current_author", "recipient": turn["task_id"]}
    _header, lane = _open_conversation(turn["room"])
    assert lane.index(FIRST) < lane.index(SECOND)
    assert [row[:3] for row in facts["rows"]] == [["in", room, FIRST], ["in", room, SECOND],
                                                 ["out", room, "Answered Ev-1"]]
    assert facts["posts"] == [["D1", "", "Answered Ev-1"]]
    assert facts["inbox"] == {"Ev-1": "delivered", "Ev-2": "delivered"}


def test_a_reply_under_ouroboros_s_channel_post_joins_the_room_the_post_was_recorded_in(tmp_path):
    facts = _run(tmp_path, "post")
    earlier, thread = facts["rooms"]["slack:T1:C1:1.0"], facts["rooms"]["slack:T1:C1:9.1"]
    # An earlier event stated the channel's kind, so the receipt of the top-level post names the thread it opens:
    # the post, the reply in that thread and the answer share one room; the earlier message keeps its own.
    assert [row[:3] for row in facts["rows"]] == [["in", earlier, "Morning, all."], ["out", thread, POST],
                                                 ["in", thread, FIRST], ["out", thread, "Answered Ev-1"]]
    [turn] = facts["turns"]
    assert turn["source"] == "Ev-1" and turn["chat_id"] == thread
    header, lane = _open_conversation(turn["room"])
    assert "people 1, mine 1, task facts 0" in header and lane.index(POST) < lane.index(FIRST)
    # Nothing a person sees changes: the post stays at the channel's top level and the answer goes to its thread.
    assert facts["posts"] == [["C1", "", POST], ["C1", "9.1", "Answered Ev-1"]]
    assert facts["inbox"] == {"Ev-0": "delivered", "Ev-1": "delivered"}


def _drive(root: Path, scenario: str) -> dict:
    """Run the bridge against core's app; return what each side recorded, as JSON-safe data."""
    import asyncio
    import sqlite3
    import threading
    import time

    import httpx

    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import InboundWorker, OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore
    from ouroboros import dialogue_store, memory_view, places, presence_admission, presence_context
    from ouroboros.chronicle_store import ChronicleStore
    from ouroboros.gateway import host_service
    from ouroboros.owner_mailbox import drain_owner_entries
    from ouroboros.presence_runner import presence_chat_id, run_presence_turn
    from ouroboros.task_results import write_task_result

    data, repo = root / "data", root / "repo"
    ChronicleStore(data).ensure_activated()  # as on a running install: rows written from now on carry their authors
    places.connect_place(data, connector="slack-bridge", provider="slack", account_id="T1", conversation_id="",
                         wakes=["direct_message", "my_thread"])
    presence_admission.focus_enabled = lambda _root: True
    presence_admission.focus_allowance = lambda _root: {"status": "available", "remaining_usd": 2, "settled_usd": 1}
    presence_context._own_work_section = lambda *_args: ""

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token == TOKEN
        return "slack-bridge", {}
    host_service._authenticated = authenticated
    turns, lock = [], threading.Lock()

    class Model:
        """The agent seam of core's runner: intake opens as the agent loop opens it, then the room core built is read."""

        def handle_task(self, task):
            source = task["metadata"]["presence"]["event"]["source_event_id"]
            opened = getattr(self, "intake_opened_callback", None)
            if opened is not None:
                opened(task)  # continuation version 2: the custody answer the loop sends before its first model call
            heard, deadline = [], time.monotonic() + 20
            while scenario == "burst" and not heard and time.monotonic() < deadline:
                heard = [entry["place_arrival"]["source_event_id"] for entry in drain_owner_entries(
                    data, task["id"], set(), include_acknowledged=True) if entry.get("provenance") == "place_event"]
                time.sleep(0.05)
            spec = memory_view.view_spec_for_task(task, data)
            room = memory_view.render_room(memory_view.capture_memory_view(data, task, spec))
            with lock:
                turns.append({"source": source, "task_id": task["id"], "chat_id": int(task["chat_id"]), "room": room,
                              "heard": heard})
            answer = f"Answered {source}"
            write_task_result(data, task["id"], "completed", metadata={
                **task["metadata"], "presence_outcome": "message", "presence_result_text": answer},
                terminal_origin="model_final", result=answer)
            return [{"type": "presence_result", "outcome": "message", "text": answer}]

    app = host_service.create_host_service_app(data, presence_runner=lambda **kwargs: run_presence_turn(
        repo_dir=repo, drive_root=data, agent_factory=lambda **_kwargs: Model(), **kwargs))
    statuses, bodies, posts = {}, {}, []

    class Host(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx.ASGITransport(app=app)

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.url.path == "/presence/turn":
                body = json.loads(request.content)
                bodies.setdefault(body["event"]["source_event_id"], body)
                statuses[body["event"]["source_event_id"]] = response.status_code
            return response

    def provider(request):
        method, params = request.url.path.rsplit("/", 1)[-1], request.url.params
        if method == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"id": params["user"], "name": "dmitriy"}})
        if method == "conversations.info":
            kind = "is_im" if params["channel"].startswith("D") else "is_channel"
            return httpx.Response(200, json={"ok": True, "channel": {"id": params["channel"], kind: True}})
        if method == "conversations.open":
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D1"}})
        if method == "conversations.replies":
            return httpx.Response(200, json={"ok": True, "messages": [{"ts": params["ts"], "text": "thread root"}],
                                             "has_more": False})
        assert method == "chat.postMessage", method
        sent = json.loads(request.content)
        posts.append([sent["channel"], sent.get("thread_ts", ""), sent.get("markdown_text") or sent.get("text")])
        return httpx.Response(200, json={"ok": True, "channel": sent["channel"], "ts": f"9.{len(posts)}"})

    store = BridgeStore(root / "slack-state")
    store.set_runtime(workspace_id="T1")

    def arrive(event_id, ts, text, *, channel="D1", kind="im", **extra):
        payload = {"type": "events_api", "envelope_id": f"env-{event_id}", "payload": {
            "event_id": event_id, "team_id": "T1", "event": {
                "type": "message", "user": "U1", "channel": channel, "channel_type": kind, "ts": ts, "text": text,
                **extra}}}
        store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))

    def send(request_id, target, text):
        """What slack_send queues for a proactive top-level message (plugin.py)."""
        store.enqueue_outbox(request_id=request_id, target=target, thread_ts="", chunks=[text],
                             origin={"kind": "tool", "task_id": "project-1"},
                             delivery_reporting_version=store.runtime_value("presence_delivery_version", 0))

    def busy(table):
        with sqlite3.connect(store.path) as db:
            db.execute(f"UPDATE {table} SET available_at=0")
            if table == "outbox":
                db.execute("UPDATE outbox SET report_available_at=0")
                return db.execute("SELECT COUNT(*) FROM outbox WHERE state IN ('pending','leased') "
                                  "OR report_state IN ('pending','reporting')").fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM inbox WHERE state IN ('pending','leased')").fetchone()[0]

    async def settle(worker, table):
        for _ in range(600):
            if not busy(table):
                return
            await worker.process_once()
            if getattr(worker, "_report_task", None) is not None:
                await asyncio.gather(worker._report_task, return_exceptions=True)
            await asyncio.sleep(0.02)

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=Host(), timeout=60.0) as host_http:
            slack = SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http)
            adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                                  skill_token=TOKEN, http_client=host_http)
            await _discover_reporting(adapter, store)
            outbound = OutboundWorker(store, slack, adapter)
            intake, observe = (InboundWorker(store, slack, adapter, staged_root=root / "staged", role=role)
                               for role in ("intake", "observe"))
            if scenario == "answer":  # Ouroboros writes first, by member ID; the person answers at top level
                send("proactive-1", "U1", PROACTIVE)
                await settle(outbound, "outbox")
                arrive("Ev-1", "6.0", FIRST)
            elif scenario == "burst":  # two top-level messages, both waiting before the first is submitted
                arrive("Ev-1", "6.0", FIRST)
                arrive("Ev-2", "7.0", SECOND)
            else:  # "post": Slack names the channel's kind, Ouroboros posts at top level, a person replies under it
                arrive("Ev-0", "1.0", "Morning, all.", channel="C1", kind="channel")
                await intake.process_once()
                send("post-1", "C1", POST)
                await settle(outbound, "outbox")
                arrive("Ev-1", "9.5", FIRST, channel="C1", kind="channel", thread_ts="9.1", parent_user_id="U_BOT")
            for _ in range(2):
                await intake.process_once()
            await settle(observe, "inbox")
            await settle(outbound, "outbox")
            await outbound.aclose()
            await adapter.aclose()
            await slack.aclose()
    asyncio.run(work())

    with sqlite3.connect(store.path) as db:
        inbox = dict(db.execute("SELECT event_id,state FROM inbox ORDER BY id"))
    attention = {}
    with dialogue_store.transaction(data, write=False) as conn:
        for row in conn.execute("SELECT client_message_id,dispatch_input FROM dialogue WHERE stream='chat' AND direction='in'"):
            attention[str(row["client_message_id"])] = json.loads(row["dispatch_input"] or "{}").get("place_attention") or {}
    rows = [[entry.row.get("direction"), int(entry.row.get("chat_id") or 0), entry.row.get("text"), entry.row.get("type")]
            for entry in dialogue_store.iter_rows(data, "chat")]
    queue = (bodies.get("Ev-1", {}).get("event", {}).get("conversation") or {}).get("transport_queue") or {}
    keys = ("slack:T1:D1:0", "slack:T1:C1:0", "slack:T1:C1:1.0", "slack:T1:C1:9.1")
    return {"rooms": {key: presence_chat_id(key) for key in keys}, "rows": rows, "turns": turns, "posts": posts,
            "statuses": statuses, "inbox": inbox, "attention": attention,
            "queue": {"conversation_key": queue.get("conversation_key"),
                      "events": [event["source_event_id"] for event in queue.get("events", [])]}}


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, default=str))
