"""Opt-in #1536 consumer with a separate real Host process and production bridge.

Run explicitly through core's safe_test.py, then /usr/bin/env
OUROBOROS_PRESENCE_CORE_ROOT=/exact/core python -m pytest -q -s /absolute/this/file.
Without that core root this reports SKIP. No Slack account or model account is used.
The synthetic HTTP provider records the model's actual request, holds acceptance
reviews until released, and supplies deterministic replies. See presence_process_host
for the exact real mechanisms and the small agent lifecycle harness boundary.
"""
from __future__ import annotations

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time

import pytest


SKILL = Path(__file__).resolve().parents[1]
HOST_SCRIPT = Path(__file__).with_name("presence_process_host.py")
TOKEN = "isolated-presence-token"
ANSWER = "The status report is complete: all three checks passed."
NEW_WORDS = "Please also mention the backup window."
CORRECTED = ANSWER + " The backup window is 02:00-03:00 UTC."
CHILD_ANSWER = "The promoted child has completed its independent report."
QUEUED_TEXTS = [f"Queued-{index}: " + "full source words " * 80 + f" end-{index}" for index in range(12)]
pytestmark = pytest.mark.serial


@pytest.mark.parametrize("mode", ["blocking", "advisory"])
@pytest.mark.parametrize("reporting", [0, 1])
def test_separate_host_delayed_review_and_production_bridge(tmp_path, mode, reporting):
    facts = _run_consumer(tmp_path, mode, reporting)
    assert facts["host_pid"] != facts["bridge_pid"] and facts["host_exit_code"] == 0
    assert facts["core_sources_unchanged"] and facts["core_source_file_count"] > 50
    assert facts["bad_token_status"] == 403
    assert facts["bad_binding_status"] in {403, 404}
    assert facts["review_was_pending_for_second_turn"]
    assert facts["replay_equal"]
    assert facts["model_calls"]["Fresh-2"] == 1
    assert facts["model_calls"]["Review-1"] in {2, 3}  # production source acknowledgement may take a round
    assert facts["resumed_model_calls"] == 1
    assert facts["replay_inference_unchanged"]
    assert facts["review_calls"] == 1
    assert facts["resumed_has_new_words"] and facts["resumed_has_source_event"]
    assert facts["no_direct_send_tool"]
    assert facts["terminal_status"] == "completed"
    assert facts["latest_turn_source"] == "Fresh-2"
    assert facts["outbox_states"] == ["delivered"] * len(facts["provider_texts"])
    assert len(facts["output_refs"]) == len(set(facts["output_refs"]))
    assert all(facts["output_refs"])
    assert facts["reporting_versions"] == [reporting] * len(facts["provider_texts"])
    if mode == "blocking":
        assert facts["before_release"] == ["Noted."]
        assert facts["provider_texts"] == ["Noted.", ANSWER]
    else:
        assert facts["before_release"] == [ANSWER, "Noted."]
        assert facts["provider_texts"] == [ANSWER, "Noted.", CORRECTED]
        assert facts["resumed_has_fail"]
        assert facts["author_disposition"] == "accepted"
        if reporting:
            assert facts["resumed_has_delivered"]


def test_six_parked_authors_release_real_host_reservations(tmp_path):
    facts = _run_consumer(tmp_path, "blocking", 1, "six")
    assert facts["host_pid"] != facts["bridge_pid"] and facts["host_exit_code"] == 0
    assert facts["core_sources_unchanged"]
    assert facts["parked_before_release"] == 6
    assert facts["review_calls"] == 6
    assert facts["before_release"] == []
    assert facts["provider_texts"] == [ANSWER] * 6
    assert facts["outbox_states"] == ["delivered"] * 6
    assert len(set(facts["output_refs"])) == 6 and all(facts["output_refs"])
    assert facts["submit_statuses"] == [200] * 6


def test_queued_facts_reach_resumed_model_through_frozen_reader(tmp_path):
    facts = _run_consumer(tmp_path, "blocking", 1, "queue")
    assert facts["host_pid"] != facts["bridge_pid"] and facts["host_exit_code"] == 0
    assert facts["core_sources_unchanged"]
    assert facts["queued_still_unsubmitted"] == 12
    assert facts["queue_source_texts"] == QUEUED_TEXTS
    assert facts["queue_source_hash_verified"]
    assert facts["queue_source_pages"] >= 2
    assert facts["queue_source_binding"] == "Review-1"
    assert facts["queue_refresh_posts"] >= 1
    assert facts["provider_texts"] == ["Noted.", ANSWER]


@pytest.mark.parametrize("dead_parent", [False, True])
def test_late_promoted_child_survives_bridge_restart_before_parent_completion(tmp_path, dead_parent):
    facts = _run_consumer(tmp_path, "blocking", 1, "dead-child" if dead_parent else "late-child")
    assert facts["host_pid"] != facts["bridge_pid"] and facts["host_exit_code"] == 0
    assert facts["core_sources_unchanged"]
    assert facts["initial_status"] == "continuing" and facts["initial_child_ref"] == ""
    assert facts["discovered_child_ref"] and facts["child_completed_before_parent"]
    assert facts["parent_discovery_status"] == ("interrupted" if dead_parent else "pending")
    assert facts["bridge_restarts"] == 2
    assert facts["original_event_posts"] == 1
    assert facts["provider_texts"] == [CHILD_ANSWER]
    assert facts["outbox_states"] == ["delivered"]
    assert facts["output_task_ids"] == [facts["discovered_child_ref"]]
    assert facts["final_inbox_state"] == ("failed" if dead_parent else "pending")
    if dead_parent:
        assert "interrupted" in facts["final_inbox_error"]
        assert len(facts["host_pids"]) == 2 and len(set(facts["host_pids"])) == 2
        assert facts["killed_host_exit_codes"] == [-9]


def test_real_host_409_preserves_admitted_child_and_original_refusal_after_restart(tmp_path):
    facts = _run_consumer(tmp_path, "blocking", 1, "refused-child")
    assert facts["host_pid"] != facts["bridge_pid"] and facts["host_exit_code"] == 0
    assert facts["core_sources_unchanged"]
    assert facts["initial_http_status"] == 409
    assert facts["original_refusal"] and facts["discovered_child_ref"]
    assert facts["refusal_preserved_exactly"]
    assert facts["bridge_restarts"] == 2
    assert facts["original_event_posts"] == 1
    assert facts["provider_texts"] == [CHILD_ANSWER]
    assert facts["outbox_states"] == ["delivered"]
    assert facts["output_task_ids"] == [facts["discovered_child_ref"]]
    assert facts["final_inbox_state"] == "failed"
    assert facts["original_refusal"] in facts["final_inbox_error"]


def _run_consumer(tmp_path, mode, reporting, scenario="main"):
    core = os.environ.get("OUROBOROS_PRESENCE_CORE_ROOT", "").strip()
    if not core:
        pytest.skip("OUROBOROS_PRESENCE_CORE_ROOT unset: separate-process consumer not exercised")
    assert (Path(core) / "ouroboros" / "presence_continuation.py").is_file()
    env = _environment(tmp_path, Path(core).resolve(), mode)
    command = [sys.executable, str(Path(__file__).resolve()), str(tmp_path), mode, str(reporting), scenario]
    # Bounded synchronous helper: terminate the driver cooperatively before the
    # final kill fallback so its finally block reaps the separately supervised Host.
    timed_out = False
    with subprocess.Popen(command, env=env, cwd=tmp_path, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    (tmp_path / "driver.stdout").write_text(result.stdout)
    (tmp_path / "driver.stderr").write_text(result.stderr)
    assert not timed_out, f"Consumer timed out; cleanup and output retained at {tmp_path}"
    assert result.returncode == 0, f"Evidence retained at {tmp_path}\n{result.stdout[-8000:]}\n{result.stderr[-8000:]}"
    facts = json.loads((tmp_path / "consumer-facts.json").read_text())
    assert facts["bridge_sources_unchanged"]
    summary = {key: value for key, value in facts.items() if key != "queue_source_texts"}
    if "queue_source_texts" in facts:
        summary["queue_text_count"] = len(facts["queue_source_texts"])
        summary["queue_text_chars"] = list(map(len, facts["queue_source_texts"]))
    print("PRESENCE_PROCESS_EVIDENCE " + json.dumps({"root": str(tmp_path), "mode": mode,
                                                    "reporting": reporting, "scenario": scenario, **summary}, sort_keys=True))
    return facts


def _environment(root: Path, core: Path, mode: str) -> dict[str, str]:
    for name in ("home", "app", "data", "repo", "projects", "worktrees", "deliverables", "bench"):
        (root / name).mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(root / "home"),
        "OUROBOROS_APP_ROOT": str(root / "app"), "OUROBOROS_DATA_DIR": str(root / "data"),
        "OUROBOROS_SETTINGS_PATH": str(root / "data" / "settings.json"),
        "OUROBOROS_REPO_DIR": str(root / "repo"), "OUROBOROS_SUBAGENT_PROJECTS_ROOT": str(root / "projects"),
        "OUROBOROS_SUBAGENT_WORKTREE_ROOT": str(root / "worktrees"),
        "OUROBOROS_DELIVERABLES_ROOT": str(root / "deliverables"), "OUROBOROS_BENCH_RUNS_ROOT": str(root / "bench"),
        "PYTHONPATH": os.pathsep.join((str(core), str(SKILL))),
        "OUROBOROS_PRESENCE_CORE_ROOT": str(core),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "MCP_ENABLED": "false",
        "OUROBOROS_PRESENCE_MAX_ACTIVE": "1", "OUROBOROS_MAX_ROUNDS": "12",
        "OUROBOROS_TASK_REVIEW_MODE": "auto", "OUROBOROS_REVIEW_ENFORCEMENT": mode,
        "OUROBOROS_REVIEW_MAX_CYCLES": "3", "OUROBOROS_SAFETY_MODE": "off",
        "OUROBOROS_REVIEWER_SLOTS": json.dumps({
            "triad": [{"slot_id": "acceptance-one", "route": {"kind": "api_chat", "target_id": "fixture/reviewer"},
                       "effort": "high", "delivery": "packet"}],
            "scope": [{"slot_id": "unused-scope", "route": {"kind": "api_chat", "target_id": "fixture/reviewer"}}],
            "advisory": {"enabled": False, "route": {"kind": "api_chat", "target_id": "fixture/reviewer"}},
        }),
    }
    return env


def _finish(identifier: str, **arguments) -> dict:
    return _tool(identifier, "presence_finish", {"outcome": "message", **arguments})


def _tool(identifier: str, name: str, arguments: dict) -> dict:
    return {"content": None, "tool_calls": [{"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}]}


class Provider:
    def __init__(self, root: Path, mode: str, scenario: str):
        self.root, self.mode, self.scenario = root, mode, scenario
        self.release, self.review_entered = threading.Event(), threading.Event()
        self.parent_release, self.parent_review_entered = threading.Event(), threading.Event()
        self.inputs, self.reviews = [], []
        self.queue_facts = {}
        self.lock = threading.Lock()

    def serve(self, path: str, body: dict) -> dict:
        if path == "/model":
            with self.lock:
                self.inputs.append(body)
                (self.root / "model-requests.json").write_text(json.dumps(self.inputs, indent=2))
            source = body["source"]
            if source == "Fresh-2":
                return _finish("fresh-answer", message="Noted.")
            assert source.startswith("Review-"), source
            resumed = "[PRESENCE CONVERSATION RESUMED]" in str(body["messages"])
            if not resumed:
                return _finish("nominate", message=ANSWER, **({"pending_review": "finish"}
                                                              if self.mode == "advisory" else {}))
            if self.scenario in {"late-child", "dead-child"}:
                return _finish("nominate-after-promotion", message=ANSWER + " The child is preparing details.")
            if self.scenario == "queue":
                # Behave as the actual consumer model: read the advertised, scoped
                # source in ordinary tool rounds. No host-side private-file shortcut.
                source_call = re.search(r'get_task_result\((\{"task_id":.*?\})\)', str(body["messages"]))
                assert source_call, "resumed model did not receive a source read affordance"
                selector = json.loads(source_call.group(1))
                pages = []
                for row in body["messages"]:
                    if row.get("role") != "tool":
                        continue
                    try:
                        content = row.get("content", "")
                        if isinstance(content, list):
                            content = "".join(item.get("text", "") for item in content)
                        source = json.loads(content).get("presence_reentry_source")
                    except (ValueError, TypeError):
                        continue
                    if source and source.get("complete_sha256") == selector["presence_reentry_sha256"]:
                        pages.append(source)
                if not pages:
                    return _tool("read-reentry-size", "get_task_result", selector)
                length = pages[-1]["complete_chars"]
                start = max((page.get("end_char", 0) for page in pages), default=0)
                if start < length:
                    return _tool(f"read-reentry-{start}", "get_task_result", {
                        **selector, "source_start_char": start, "source_end_char": min(start + 12000, length)})
                text = "".join(page["text"] for page in pages if "text" in page)
                assert hashlib.sha256(text.encode()).hexdigest() == selector["presence_reentry_sha256"]
                observation = json.loads(text)["transport_queue"]
                assert observation["status"] == "available", observation
                snapshot = observation["snapshot"]
                self.queue_facts = {"queue_source_texts": [event["text"] for event in snapshot["events"]],
                                    "queue_source_hash_verified": True,
                                    "queue_source_pages": sum("text" in page for page in pages),
                                    "queue_source_binding": snapshot["after_source_event_id"]}
            if self.mode == "advisory":
                return _finish("correct", message=CORRECTED, author_disposition="accepted",
                               rationale="Added the backup window requested by the new message and review.")
            return _finish("final", answer_sha256=hashlib.sha256(ANSWER.encode()).hexdigest())
        assert path == "/review", path
        with self.lock:
            self.reviews.append(body)
            review_number = len(self.reviews)
            (self.root / "review-requests.json").write_text(json.dumps(self.reviews, indent=2))
        if self.scenario in {"late-child", "dead-child"} and review_number > 1:
            self.parent_review_entered.set()
            assert self.parent_release.wait(90), "test did not release the promoted author's review"
        else:
            self.review_entered.set()
            assert self.release.wait(90), "test did not release synthetic review"
        failed = self.mode == "advisory" or (self.scenario in {"late-child", "dead-child"} and review_number == 1)
        return {"verdict": "FAIL" if failed else "PASS", "summary": "Independent review",
                "findings": [], "outcome_tier": "best_effort" if failed else "solved",
                "completion_coach": "Mention the backup." if self.mode == "advisory" else "Deliver it.",
                "criteria_used": [{"criterion": "complete report", "status": "supported",
                                   "evidence_refs": [body["ref"]]}]}


def _server(provider: Provider):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                result = provider.serve(self.path, body)
                payload = json.dumps(result).encode()
                self.send_response(200)
            except Exception as exc:
                payload = json.dumps({"error": repr(exc)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _wait(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("consumer condition did not settle")


def _drive(root: Path, mode: str, reporting: int, scenario: str) -> dict:
    import httpx
    from ouroboros.process_custody import spawn_supervised

    core = Path(os.environ["OUROBOROS_PRESENCE_CORE_ROOT"])
    before = {str(path.relative_to(core)): hashlib.sha256(path.read_bytes()).hexdigest()
              for dirname in ("ouroboros", "supervisor") for path in (core / dirname).rglob("*.py")}
    bridge_before = {str(path.relative_to(SKILL)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in SKILL.rglob("*.py")}
    provider = Provider(root, mode, scenario)
    server, thread = _server(provider)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    with (root / "host.log").open("w") as log:
        process = spawn_supervised([sys.executable, str(HOST_SCRIPT), str(root), url, mode, scenario],
                                   drive_root=root / "data", purpose="presence-consumer-fixture", scope="session",
                                   cwd=root, env=dict(os.environ), stdout=log, stderr=subprocess.STDOUT)
        try:
            _wait(lambda: (root / "host-address.json").is_file() or process.poll() is not None)
            assert process.poll() is None, (root / "host.log").read_text()
            host_url = json.loads((root / "host-address.json").read_text())["url"]

            def ready():
                try:
                    with httpx.Client(trust_env=False, timeout=1) as client:
                        return client.get(host_url + "/fixture-ready").json()
                except (httpx.HTTPError, ValueError):
                    return None

            identity = _wait(ready)
            host_pids = [process.pid]
            killed_exit_codes, used_modules = [], set()

            def restart_host():
                # Kill the actual parked author process; its canonical row stays
                # RUNNING. Core's controller witness, not a fabricated status,
                # must project the lost author as interrupted after restart.
                nonlocal process, host_url
                with httpx.Client(trust_env=False, timeout=5) as client:
                    used_modules.update(client.get(host_url + "/fixture-sources").json())
                process.kill()
                process.wait(timeout=5)
                killed_exit_codes.append(process.returncode)
                (root / "host-address.json").unlink()
                process = spawn_supervised(
                    [sys.executable, str(HOST_SCRIPT), str(root), url, mode, scenario],
                    drive_root=root / "data", purpose="presence-consumer-fixture", scope="session",
                    cwd=root, env=dict(os.environ), stdout=log, stderr=subprocess.STDOUT)
                _wait(lambda: (root / "host-address.json").is_file() or process.poll() is not None)
                assert process.poll() is None, (root / "host.log").read_text()
                host_url = json.loads((root / "host-address.json").read_text())["url"]
                restarted = _wait(ready)
                host_pids.append(process.pid)
                return host_url, restarted

            facts = {"host_pid": process.pid, "bridge_pid": os.getpid(),
                     **asyncio.run(_consume(root, host_url, identity, provider, reporting, scenario,
                                          restart_host=restart_host)), "host_pids": host_pids,
                     "killed_host_exit_codes": killed_exit_codes}
            with httpx.Client(trust_env=False, timeout=5) as client:
                used_modules.update(client.get(host_url + "/fixture-sources").json())
            used = sorted(used_modules)
            (root / "host-core-modules.json").write_text(json.dumps(used, indent=2))
        except BaseException:
            print((root / "host.log").read_text(), file=sys.stderr)
            raise
        finally:
            provider.release.set()
            provider.parent_release.set()
            if process.poll() is None:
                try:
                    with httpx.Client(trust_env=False, timeout=2) as client:
                        client.post(host_url + "/fixture-shutdown")
                except (httpx.HTTPError, UnboundLocalError):
                    process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            (root / "process-exit.json").write_text(json.dumps({"pid": process.pid, "returncode": process.returncode}))
    used = json.loads((root / "host-core-modules.json").read_text())
    source_manifest = {path: {"before": before.get(path), "after": hashlib.sha256((core / path).read_bytes()).hexdigest()}
                       for path in used if path.endswith(".py")}
    manifest_text = json.dumps({"core_root": str(core), "files": source_manifest}, indent=2, sort_keys=True)
    (root / "core-source-manifest.json").write_text(manifest_text)
    bridge_paths = {str(Path(module.__file__).resolve().relative_to(SKILL)) for module in sys.modules.values()
                    if getattr(module, "__file__", None) and Path(module.__file__).resolve().is_relative_to(SKILL)}
    bridge_paths.add(str(HOST_SCRIPT.relative_to(SKILL)))
    bridge_manifest = {path: {"before": bridge_before.get(path),
                              "after": hashlib.sha256((SKILL / path).read_bytes()).hexdigest()}
                       for path in sorted(bridge_paths) if path.endswith(".py")}
    bridge_text = json.dumps({"bridge_root": str(SKILL), "files": bridge_manifest}, indent=2, sort_keys=True)
    (root / "bridge-source-manifest.json").write_text(bridge_text)
    facts.update(core_sources_unchanged=all(row["before"] == row["after"] for row in source_manifest.values()),
                 core_source_file_count=len(source_manifest),
                 host_exit_code=process.returncode,
                 core_manifest_sha256=hashlib.sha256(manifest_text.encode()).hexdigest())
    facts.update(bridge_sources_unchanged=all(row["before"] == row["after"] for row in bridge_manifest.values()),
                 bridge_source_file_count=len(bridge_manifest),
                 bridge_manifest_sha256=hashlib.sha256(bridge_text.encode()).hexdigest())
    return facts


async def _consume(root: Path, host_url: str, identity: dict, provider: Provider, reporting: int, scenario: str,
                   *, restart_host=None) -> dict:
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import InboundWorker, OutboundWorker
    from lib.slack_api import SlackClient
    from lib.socket_mode import SocketModeClient
    from lib.store import BridgeStore
    from ouroboros.presence_runner import _read_previous_turn
    from ouroboros.task_results import load_task_result

    posted, submitted, submit_statuses, queue_posts, wire_responses = [], [], [], [], []

    def slack_api(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"id": request.url.params["user"], "name": "reader"}})
        if method == "conversations.info":
            return httpx.Response(200, json={"ok": True, "channel": {"id": request.url.params["channel"]}})
        assert method == "chat.postMessage", method
        body = json.loads(request.content)
        posted.append(body)
        return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": f"9.{len(posted)}"})

    async def observe(request):
        if request.url.path == "/presence/turn":
            submitted.append(json.loads(request.content))
        if request.method == "POST" and request.url.path.startswith("/presence/work/"):
            queue_posts.append(json.loads(request.content))

    async def observe_response(response):
        if response.request.url.path == "/presence/turn":
            submit_statuses.append(response.status_code)
        if response.request.url.path == "/presence/turn" or (
                response.request.method == "GET" and response.request.url.path.startswith("/presence/work/")):
            await response.aread()
            wire_responses.append({"path": response.request.url.path,
                                   "status_code": response.status_code, "body": response.json()})

    store = BridgeStore(root / "bridge")
    store.set_runtime(workspace_id="T1")
    headers = {"X-Skill-Token": TOKEN}
    async with httpx.AsyncClient(trust_env=False, timeout=60,
                                 event_hooks={"request": [observe], "response": [observe_response]}) as host_http, \
            httpx.AsyncClient(transport=httpx.MockTransport(slack_api)) as slack_http:
        adapter = LoopbackPresenceHostAdapter(binding_id=identity["binding"], host_service_url=host_url,
                                              skill_token=TOKEN, http_client=host_http)
        await adapter.discover_delivery_support()
        assert adapter.continuation_version == 1
        # The consumer may opt out of receipt reporting while negotiating continuation.
        adapter.delivery_reporting_version = reporting
        adapter.delivery_reporting_status = "supported" if reporting else "unsupported"
        slack = SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http)
        socket = SocketModeClient(slack, store, bot_user_id="U_BOT")
        inbound = InboundWorker(store, slack, adapter, staged_root=root / "bridge" / "staged")
        outbound = OutboundWorker(store, slack, adapter)

        class Websocket:
            async def send(self, _value):
                pass

        async def ingest(event_id, ts, text, thread_ts="1.0"):
            envelope = {"type": "events_api", "envelope_id": "env-" + event_id, "payload": {
                "event_id": event_id, "team_id": "T1", "event": {"type": "message", "user": "U1",
                "channel": "C1", "channel_type": "channel", "ts": ts, "thread_ts": thread_ts, "text": text}}}
            await socket.handle_raw_message(Websocket(), json.dumps(envelope))

        def due(*, inbox=True):
            with sqlite3.connect(store.path) as db:
                if inbox:
                    db.execute("UPDATE inbox SET available_at=0")
                db.execute("UPDATE outbox SET available_at=0, report_available_at=0")

        def rows(table):
            with sqlite3.connect(store.path) as db:
                db.row_factory = sqlite3.Row
                return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]

        async def flush():
            for _ in range(30):
                due(inbox=False)
                worked = await outbound.process_once()
                if outbound._report_task is not None:
                    await asyncio.gather(outbound._report_task)
                if not worked:
                    return
            raise AssertionError("outbound did not settle")

        async def restart_bridge():
            # Reopen the durable store and discard every adapter/worker cache.
            # HTTP connections and the synthetic provider do not own bridge state.
            nonlocal store, adapter, slack, socket, inbound, outbound
            await outbound.aclose()
            await adapter.aclose()
            await slack.aclose()
            store = BridgeStore(root / "bridge")
            adapter = LoopbackPresenceHostAdapter(binding_id=identity["binding"], host_service_url=host_url,
                                                  skill_token=TOKEN, http_client=host_http)
            await adapter.discover_delivery_support()
            adapter.delivery_reporting_version = reporting
            adapter.delivery_reporting_status = "supported" if reporting else "unsupported"
            slack = SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http)
            socket = SocketModeClient(slack, store, bot_user_id="U_BOT")
            inbound = InboundWorker(store, slack, adapter, staged_root=root / "bridge" / "staged")
            outbound = OutboundWorker(store, slack, adapter)

        try:
            if scenario in {"late-child", "dead-child", "refused-child"}:
                await ingest("Review-1", "1.0", "Please prepare the status report.")
                assert await inbound.process_once()
                initial = next(row for row in wire_responses if row["path"] == "/presence/turn")
                first = initial["body"]
                retained = rows("inbox")[0]["host_reference"]
                assert retained, rows("inbox")
                assert rows("inbox")[0]["state"] == "pending", rows("inbox")

                async def child_state(predicate):
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        state = (await host_http.get(host_url + "/fixture-child")).json()
                        if state.get("ready") and predicate(state):
                            return state
                        await asyncio.sleep(0.05)
                    raise AssertionError("child lifecycle did not reach the required state")

                if scenario == "refused-child":
                    assert initial["status_code"] == 409, initial
                    assert first["code"] == "presence_resources_unavailable", first
                    assert first["disposition"] == "retry", first
                    assert first["work_ref"] and not first.get("text"), first
                    state = await child_state(lambda _state: True)
                    await restart_bridge()
                    assert rows("inbox")[0]["host_reference"] == retained
                    due()
                    assert await inbound.process_once()  # still pending, poll only after restart
                    assert len(submitted) == 1, submitted
                else:
                    assert first["status"] == "continuing" and first["work_ref"] == "", first
                    assert provider.review_entered.is_set() and not provider.release.is_set()
                    provider.release.set()
                    state = await child_state(lambda value: provider.parent_review_entered.is_set() and (
                        value["parent"]["presence_continuation"]["lent_at"]
                        != value["parent"]["presence_continuation"]["first_lent_at"]))
                    assert state["parent"]["status"] == "running", state
                    assert state["parent"]["presence_continuation"]["initial"]["work_ref"] == ""
                completed = (await host_http.post(host_url + "/fixture-child",
                                                  json={"action": "complete"})).json()
                assert completed["child"]["status"] == "completed", completed
                if scenario == "dead-child":
                    assert restart_host is not None
                    host_url, identity = restart_host()
                if scenario != "refused-child":
                    await restart_bridge()
                due()
                assert await inbound.process_once()
                await flush()
                assert [body["markdown_text"] for body in posted] == [CHILD_ANSWER], wire_responses
                author_polls = [row["body"] for row in wire_responses
                                if row["path"] == "/presence/work/" + first["turn_ref"]]
                discovered = first["work_ref"] if scenario == "refused-child" else author_polls[-1].get("child_work_ref")
                assert discovered == state["work_ref"], (discovered, state)
                await restart_bridge()
                await ingest("Review-1", "1.0", "Please prepare the status report.")
                for _ in range(3):
                    due()
                    await inbound.process_once()
                    await flush()
                inbox, outbox = rows("inbox")[0], rows("outbox")
                refusal_preserved = False
                if scenario == "refused-child":
                    from lib.host_adapter import _decode_refused_reference
                    refusal = _decode_refused_reference(inbox["host_reference"])
                    refusal_preserved = (inbox["host_reference"] == retained
                                         and refusal["http_status"] == initial["status_code"]
                                         and refusal["response"] == first)
                facts = {"initial_status": first.get("status"), "initial_child_ref": first.get("work_ref", ""),
                         "initial_http_status": initial["status_code"],
                         "original_refusal": first.get("code", ""),
                         "refusal_preserved_exactly": refusal_preserved,
                         "discovered_child_ref": discovered,
                         "parent_discovery_status": author_polls[-1]["status"] if author_polls else "",
                         "child_completed_before_parent": completed["parent"]["status"] == "running",
                         "bridge_restarts": 2, "restart_boundary": "store, adapter, workers and transport objects",
                         "original_event_posts": len(submitted),
                         "provider_texts": [body["markdown_text"] for body in posted],
                         "outbox_states": [row["state"] for row in outbox],
                         "output_task_ids": [json.loads(row["origin_json"])["task_id"] for row in outbox],
                         "final_inbox_state": inbox["state"], "final_inbox_error": inbox["last_error"]}
                (root / "wire-turns.json").write_text(json.dumps(submitted, indent=2))
                (root / "outbox.json").write_text(json.dumps(outbox, indent=2))
                (root / "final-inbox.json").write_text(json.dumps(inbox, indent=2))
                return facts
            if scenario == "six":
                for index in range(6):
                    await ingest(f"Review-{index}", f"1.{index}", "Please prepare the status report.", f"1.{index}")
                    assert await inbound.process_once()
                assert len(provider.reviews) == 6 and not provider.release.is_set()
                parked = rows("inbox")
                assert all(row["state"] == "pending" for row in parked), parked
                before = [body["markdown_text"] for body in posted]
                provider.release.set()
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    due()
                    await inbound.process_once()
                    await flush()
                    if all(row["state"] == "delivered" for row in rows("inbox")):
                        break
                    await asyncio.sleep(0.05)
                assert all(row["state"] == "delivered" for row in rows("inbox")), rows("inbox")
                outbox = rows("outbox")
                (root / "wire-turns.json").write_text(json.dumps(submitted, indent=2))
                (root / "outbox.json").write_text(json.dumps(outbox, indent=2))
                return {"parked_before_release": len(parked), "review_calls": len(provider.reviews),
                        "before_release": before, "provider_texts": [body["markdown_text"] for body in posted],
                        "outbox_states": [row["state"] for row in outbox],
                        "output_refs": [row["output_ref"] for row in outbox], "submit_statuses": submit_statuses}
            bad_token = await host_http.get(host_url + "/identity", headers={"X-Skill-Token": "wrong"})
            await ingest("Review-1", "1.0", "Please prepare the status report.")
            assert await inbound.process_once()
            assert provider.review_entered.is_set() and not provider.release.is_set()
            first_body = submitted[0]
            first = (await host_http.post(host_url + "/presence/turn", headers=headers, json=first_body)).json()
            assert first["status"] == "continuing", first
            bad_binding = await host_http.get(host_url + "/presence/work/" + first["turn_ref"],
                                             headers=headers, params={"binding_id": "f" * 32})
            await flush()
            await ingest("Fresh-2", "2.0", NEW_WORDS)
            if scenario == "queue":
                for index, text in enumerate(QUEUED_TEXTS):
                    await ingest(f"Queued-{index}", f"3.{index}", text)
            assert await inbound.process_once()
            await flush()
            assert dict((row["event_id"], row["state"]) for row in rows("inbox"))["Fresh-2"] == "delivered"
            assert not provider.release.is_set()
            before = [body["markdown_text"] for body in posted]
            if scenario == "queue":
                due()
                assert await inbound.process_once()  # production poll POSTs full current queue before GET
            provider.release.set()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                due()
                await inbound.process_once()
                await flush()
                if all(row["state"] == "delivered" for row in rows("inbox")
                       if not row["event_id"].startswith("Queued-")):
                    break
                await asyncio.sleep(0.05)
            assert all(row["state"] == "delivered" for row in rows("inbox")
                       if not row["event_id"].startswith("Queued-")), rows("inbox")
            calls_before_replay = len(provider.inputs)
            replay = (await host_http.post(host_url + "/presence/turn", headers=headers, json=first_body)).json()
            await ingest("Review-1", "1.0", "Please prepare the status report.")
            for _ in range(0 if scenario == "queue" else 3):
                due()
                await inbound.process_once()
                await flush()
            inputs = [row for row in provider.inputs if row["source"] == "Review-1"]
            resumed = str(inputs[-1]["messages"])
            task = load_task_result(root / "data", first["turn_ref"])
            latest = _read_previous_turn(root / "data", first_body["event"]["conversation_key"])
            latest_row = load_task_result(root / "data", latest["task_id"])
            outbox = rows("outbox")
            facts = {"bad_token_status": bad_token.status_code, "bad_binding_status": bad_binding.status_code,
                     "review_was_pending_for_second_turn": True, "replay_equal": replay == first,
                     "before_release": before, "provider_texts": [body["markdown_text"] for body in posted],
                     "model_calls": {source: sum(row["source"] == source for row in provider.inputs)
                                     for source in {row["source"] for row in provider.inputs}},
                     "resumed_model_calls": sum("[PRESENCE CONVERSATION RESUMED]" in str(row["messages"])
                                                 for row in inputs),
                     "replay_inference_unchanged": len(provider.inputs) == calls_before_replay,
                     "review_calls": len(provider.reviews), "resumed_has_new_words": NEW_WORDS in resumed,
                     "resumed_has_source_event": "Fresh-2" in resumed,
                     "resumed_has_fail": "acceptance-one: FAIL" in resumed,
                     "resumed_has_delivered": "delivery delivered for this turn" in resumed,
                     "no_direct_send_tool": all("slack_send_message" not in row["tools"] for row in provider.inputs),
                     "terminal_status": task["status"],
                     "latest_turn_source": latest_row["metadata"]["presence"]["event"]["source_event_id"],
                     "outbox_states": [row["state"] for row in outbox],
                     "output_refs": [row["output_ref"] for row in outbox],
                     "reporting_versions": [row["delivery_reporting_version"] for row in outbox],
                     "author_disposition": ((task.get("review_status") or {}).get("acceptance_decision") or {})
                         .get("author_disposition", {}).get("disposition", "")}
            if scenario == "queue":
                facts.update(provider.queue_facts, queue_refresh_posts=len(queue_posts),
                             queued_still_unsubmitted=sum(row["event_id"].startswith("Queued-")
                                                          and not row["host_reference"] for row in rows("inbox")))
            (root / "turn-record.json").write_text(json.dumps(task, indent=2))
            (root / "wire-turns.json").write_text(json.dumps(submitted, indent=2))
            (root / "wire-queue-refreshes.json").write_text(json.dumps(queue_posts, indent=2))
            (root / "outbox.json").write_text(json.dumps(outbox, indent=2))
            return facts
        finally:
            (root / "wire-responses.json").write_text(json.dumps(wire_responses, indent=2))
            await outbound.aclose()
            await adapter.aclose()
            await slack.aclose()


if __name__ == "__main__":
    import signal

    def terminate_driver(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate_driver)
    output_root = Path(sys.argv[1])
    outcome = _drive(output_root, sys.argv[2], int(sys.argv[3]), sys.argv[4])
    (output_root / "consumer-facts.json").write_text(json.dumps(outcome, indent=2))
