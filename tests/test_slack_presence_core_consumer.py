"""Slack inbound attachments through the real core Presence route (local opt-in).

Set OUROBOROS_PRESENCE_CORE_ROOT to an Ouroboros core checkout and run this file
explicitly with a Python that has the core requirements:

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_slack_presence_core_consumer.py

Without it the test is reported as skipped, never as passed. The Slack bridge runs
unchanged (parser, store, worker, SlackClient, loopback Host adapter); Slack HTTP is
a mock transport and the Host is core's own ``/presence/turn`` app over ASGI. Only
skill-token authentication, binding admission and inference are stubbed; route
validation, staged-file confinement, attachment staging, task building, context
rendering and source identity are core code. The run happens in a subprocess whose
HOME, data, settings and repo roots all live in a temporary directory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "slack-bridge"
CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"
BINDING = "c" * 32
TOKEN = "slack-skill-token"


def test_slack_attachment_facts_reach_the_real_presence_route(tmp_path):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; the core Presence route was not exercised")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"),
        "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"),
        "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": os.pathsep.join([str(Path(core).resolve()), str(SKILL_ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir()
    run = subprocess.run([sys.executable, __file__, str(tmp_path)], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    facts = json.loads(run.stdout.strip().splitlines()[-1])

    # The bridge staged only the Slack-hosted file and never asked another host.
    assert facts["file_requests"] == ["files.slack.com/files-pri/T1-F_PDF/download/brief.pdf",
                                      "files.slack.com/files-pri/T1-F_EDGE/download/edge.pdf"]
    assert facts["agent_calls"] == ["Ev-1", "Ev-3", "Ev-2"]
    assert facts["turn_posts"] == ["Ev-1", "Ev-3", "Ev-1", "Ev-2"]
    assert facts["identical_resubmission"] is True
    assert facts["inbox_states"] == {"Ev-1": "delivered", "Ev-2": "delivered", "Ev-3": "delivered"}
    # The lost reply's turn is answered from core's durable result, not run again.
    assert facts["outbox"] == ["Seen Ev-3", "Seen Ev-1", "Seen Ev-2"]

    first = facts["turns"]["Ev-1"]
    # Core staged the bridge's bytes under the label the bridge reports as staged_as.
    by_id = {item["file_id"]: item for item in first["context_attachments"]}
    assert list(by_id) == ["F_DRIVE", "F_PDF", "F_STUB", "F_EDGE"]
    assert by_id["F_PDF"]["content_available"] is True and by_id["F_PDF"]["staged_as"] == "01-brief.pdf"
    # A name cut at 180 characters still reaches core's staging as written; core
    # may shorten the label it shows, never the bytes it opens.
    edge = by_id["F_EDGE"]["staged_as"]
    assert edge == "03-" + "a" * 179 and by_id["F_EDGE"]["content_available"] is True
    [pdf_row, edge_row] = first["manifest"]
    assert (pdf_row["status"], pdf_row["label"]) == ("staged", "01-brief.pdf")
    assert edge_row["status"] == "staged" and edge.startswith(edge_row["label"]), edge_row
    assert "[ATTACHMENTS]" in first["text"] and "01-brief.pdf" in first["text"]
    assert by_id["F_DRIVE"]["stage_error"] == "private_file_host_not_allowed"
    assert by_id["F_DRIVE"]["external_url"] == "https://docs.google.com/document/d/doc-1/edit"
    assert by_id["F_STUB"]["stage_error"] == "missing_private_file_url"

    external_only = facts["turns"]["Ev-3"]
    assert external_only["manifest"] == [] and external_only["text"] == "(empty presence event)"
    assert [item["stage_error"] for item in external_only["context_attachments"]] == [
        "private_file_host_not_allowed"]
    assert "supplied no text" in external_only["framed"]

    for turn in facts["turns"].values():
        assert turn["identity_matches_without_message"] is True
        assert not any(marker in turn["context"] for marker in ("url_private", "files-pri", "slack-files.com"))

    if facts["continuation_version"] == 1:
        # A #1536 core: every submission negotiated continuation, and core kept each event's
        # bridge-queue snapshot with the event in its canonical log and the model's context.
        assert facts["turn_continuation"] == [1, 1, 1, 1]
        assert facts["logged_queue"] == {"Ev-1": ["Ev-2"], "Ev-2": [], "Ev-3": []}
        assert '"transport_queue"' in first["context"] and '"after_source_event_id": "Ev-1"' in first["context"]
    else:
        assert facts["turn_continuation"] == [0, 0, 0, 0] and facts["logged_queue"] == {}


def _drive(root: Path) -> dict:
    """Run the bridge against core's app; return observed facts as JSON-safe data."""
    import asyncio
    import sqlite3
    import threading

    import httpx

    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import InboundWorker
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore
    from ouroboros.gateway import host_service
    from ouroboros.presence_admission import PresenceAdmission
    from ouroboros.presence_authority import PresenceCapabilityCeiling, PresenceToolGrant, presence_ceiling_payload
    from ouroboros.presence_bindings import PresenceEndpoint
    from ouroboros.presence_context import build_presence_context_section, frame_presence_user_content
    from ouroboros.presence_runner import PresenceTurnEvent, presence_event_identity, run_presence_turn
    from ouroboros.task_results import write_task_result

    data, repo = root / "data", root / "repo"
    endpoint = PresenceEndpoint("slack", "T1", "*", "")
    ceiling = PresenceCapabilityCeiling(
        skill_name="slack-helper", skill_content_hash="a" * 64, profile_fingerprint="b" * 64,
        state_fingerprint="c" * 64, selection_fingerprint="d" * 64, model_slot="main", inline_max_rounds=4,
        tool_grants=(PresenceToolGrant("chat_history"),), resource_grants=(), digest="0" * 64)
    ceiling = PresenceCapabilityCeiling(**{**ceiling.__dict__, "digest": presence_ceiling_payload(ceiling)["digest"]})
    admission = PresenceAdmission(
        binding_id=BINDING, transport_skill="slack-bridge", behavior_skill="slack-helper", origin=endpoint,
        destination=endpoint, instructions="Participate helpfully.", context_topics=(), model_slot="main",
        inline_max_rounds=4, skill_content_hash="a" * 64, profile_fingerprint="b" * 64,
        state_fingerprint="c" * 64, selection_fingerprint="d" * 64, capability_ceiling=ceiling)

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token == TOKEN and permission in {"", "presence"}
        return "slack-bridge", {}

    def admit(_ctx, skill_name, binding_id):
        assert (skill_name, binding_id) == ("slack-bridge", BINDING)
        return admission

    host_service._authenticated = authenticated
    host_service._admit_presence = admit

    turns: dict[str, dict] = {}
    agent_calls: list[str] = []
    lock = threading.Lock()

    class Agent:
        def handle_task(self, task):
            presence = task["metadata"]["presence"]
            event = presence["event"]
            context = build_presence_context_section(data, presence, task["id"])
            rendered = json.loads(context.split("## Current presence event (host-authored facts)\n\n", 1)[1])
            without_message = PresenceTurnEvent(
                source_event_id=event["source_event_id"], provider=event["provider"],
                account_id=event["account_id"], conversation_id=event["conversation_id"],
                thread_id=event["thread_id"], conversation_key=event["conversation_key"],
                actor=event["actor"], conversation={}, message={}, text=presence["observed_text"])
            reply = f"Seen {event['source_event_id']}"
            with lock:
                agent_calls.append(event["source_event_id"])
                turns[event["source_event_id"]] = {
                    "text": task["text"],
                    "manifest": [{"status": row.get("status"), "label": row.get("label"),
                                  "reason": row.get("reason")} for row in task.get("attachments") or []],
                    "context": context,
                    "context_attachments": rendered["event"]["message"]["attachments"],
                    "framed": frame_presence_user_content(task, task["text"]),
                    "identity_matches_without_message": (
                        presence_event_identity(BINDING, without_message)
                        == task["metadata"]["presence_event_identity"]),
                }
            write_task_result(data, task["id"], "completed", metadata=task["metadata"], result=reply)
            return [{"type": "presence_result", "outcome": "message", "text": reply, "work_ref": ""}]

    def runner(**kwargs):
        return run_presence_turn(repo_dir=repo, drive_root=data, agent_factory=lambda **_: Agent(), **kwargs)

    app = host_service.create_host_service_app(data, presence_runner=runner)
    turn_bodies: list[dict] = []

    class LoseFirstTurnReply(httpx.AsyncBaseTransport):
        """Core completes the first turn; its HTTP reply never reaches the bridge."""

        def __init__(self) -> None:
            self.inner, self.lost = httpx.ASGITransport(app=app), False

        async def handle_async_request(self, request):
            if request.url.path == "/presence/turn":
                turn_bodies.append(json.loads(request.content))
            response = await self.inner.handle_async_request(request)
            if request.url.path == "/presence/turn" and not self.lost:
                self.lost = True
                await response.aread()
                raise httpx.ReadTimeout("reply lost after Host completion", request=request)
            return response

    file_requests: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slack.com":
            params = request.url.params
            if request.url.path.endswith("users.info"):
                return httpx.Response(200, json={"ok": True, "user": {"id": params["user"], "name": "reader"}})
            return httpx.Response(200, json={"ok": True, "channel": {"id": params["channel"]}})
        file_requests.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(200, content=b"%PDF-1.4 synthetic")

    drive = {"id": "F_DRIVE", "name": "Roadmap", "mimetype": "application/vnd.google-apps.document",
             "mode": "external", "is_external": True, "external_type": "gdrive",
             "external_url": "https://docs.google.com/document/d/doc-1/edit",
             "url_private": "https://docs.google.com/document/d/doc-1/edit",
             "permalink_public": "https://slack-files.com/T1-F_DRIVE-secret"}
    pdf = {"id": "F_PDF", "name": "brief.pdf", "mimetype": "application/pdf", "size": 18,
           "url_private": "https://files.slack.com/files-pri/T1-F_PDF/brief.pdf",
           "url_private_download": "https://files.slack.com/files-pri/T1-F_PDF/download/brief.pdf"}
    stub = {"id": "F_STUB", "file_access": "check_file_info"}
    edge = {"id": "F_EDGE", "name": "a" * 179 + " tail.pdf", "mimetype": "application/pdf", "size": 18,
            "url_private": "https://files.slack.com/files-pri/T1-F_EDGE/download/edge.pdf"}

    def envelope(event_id, *, ts, channel, files=(), text="", thread_ts=""):
        event = {"type": "message", "user": "U1", "channel": channel, "channel_type": "channel",
                 "ts": ts, "text": text, **({"files": list(files)} if files else {}),
                 **({"thread_ts": thread_ts} if thread_ts else {})}
        return {"type": "events_api", "envelope_id": f"env-{event_id}",
                "payload": {"event_id": event_id, "team_id": "T1", "event": event}}

    store = BridgeStore(data / "state" / "skills" / "slack-bridge")
    for payload in (envelope("Ev-1", ts="1.0", channel="C1", files=[drive, pdf, stub, edge]),
                    envelope("Ev-2", ts="2.0", channel="C1", thread_ts="1.0", text="and?"),
                    envelope("Ev-3", ts="3.0", channel="C2", files=[drive])):
        store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))

    async def work(steps: int) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=transport, timeout=60.0) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            adapter = LoopbackPresenceHostAdapter(binding_id=BINDING, host_service_url="http://127.0.0.1:8767",
                                                  skill_token=TOKEN, http_client=host_http)
            worker = InboundWorker(store, slack, adapter, staged_root=store.state_dir / "staged")
            for _ in range(steps):
                await worker.process_once()

    transport = LoseFirstTurnReply()
    asyncio.run(work(3))  # Ev-1 reply lost and backed off; Ev-2 waits; Ev-3 runs
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE inbox SET available_at=0 WHERE event_id='Ev-1'")
    asyncio.run(work(2))  # Ev-1 resubmits from its checkpoint and replays; then Ev-2

    with sqlite3.connect(store.path) as db:
        states = dict(db.execute("SELECT event_id, state FROM inbox"))
        outbox = [row[0] for row in db.execute("SELECT text FROM outbox ORDER BY id")]
    posted = [body["event"]["source_event_id"] for body in turn_bodies]
    logged_queue = {}
    for line in (data / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        queue = ((row.get("transport") or {}).get("conversation") or {}).get("transport_queue")
        if row.get("direction") == "in" and queue is not None:
            logged_queue[row["client_message_id"]] = [event["source_event_id"] for event in queue["events"]]
    return {
        "file_requests": file_requests, "agent_calls": agent_calls, "turn_posts": posted,
        "identical_resubmission": turn_bodies[0] == turn_bodies[2], "inbox_states": states,
        "outbox": outbox, "turns": turns, "continuation_version": store.runtime_value("presence_continuation_version", 0),
        "turn_continuation": [body.get("continuation_version", 0) for body in turn_bodies],
        "logged_queue": logged_queue,
    }


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1])), ensure_ascii=False, default=str))
