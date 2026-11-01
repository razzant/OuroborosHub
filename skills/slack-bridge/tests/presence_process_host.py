"""Isolated Host executable for test_presence_process_consumer (not a production entry point).

The HTTP Host, token validation, binding admission, turn gate, continuation,
acceptance coordinator, tool registry, LLM loop and terminal pipeline are production
code. LoopAgent is a small lifecycle harness, not OuroborosAgent's full bootstrap.
Only inference and the review-route executor cross to the synthetic localhost
provider. The child scenarios additionally supply a supervisor admission receipt
and child terminal through this lifecycle harness; they do not dispatch a real
subagent. No production auth/admission/continuation/HTTP function is replaced.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
from types import SimpleNamespace


TOKEN = "isolated-presence-token"
CHILD_REF = "fixture-promoted-child"
CHILD_ANSWER = "The promoted child has completed its independent report."


def seed_installation(data: Path) -> str:
    """Seed synthetic owner configuration; execute real admission against it."""
    from ouroboros.gateway.host_service import AUTH_TOKEN_FILENAME
    from ouroboros.presence_bindings import PresenceBinding, PresenceEndpoint, save_presence_binding
    from ouroboros.presence_capabilities import (
        PresenceSelection, PresenceState, PresenceToolTarget, presence_state_fingerprint, save_presence_state,
    )
    from ouroboros.presence_profile import parse_presence_profile, presence_request_fingerprint
    from ouroboros.skill_loader import (
        SkillReviewState, compute_content_hash, load_skill, save_enabled, save_review_state, save_skill_grants,
    )
    from ouroboros.utils import atomic_write_json

    skill = data / "skills" / "external" / "slack-bridge"
    if skill.exists():
        # A deliberate Host restart reuses this fixture's admitted installation.
        from ouroboros.presence_bindings import load_presence_binding
        return load_presence_binding(data, "slack-bridge", "d" * 32).binding_id
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: slack-bridge\ndescription: Synthetic transport identity.\nversion: 0.1\n"
        "type: extension\nentry: plugin.py\npermissions: [presence]\n---\nFixture\n")
    (skill / "plugin.py").write_text("def register(api): pass\n")
    content_hash = compute_content_hash(skill, manifest_entry="plugin.py")
    save_review_state(data, "slack-bridge", SkillReviewState(status="pass", content_hash=content_hash))
    save_enabled(data, "slack-bridge", True)
    save_skill_grants(data, "slack-bridge", [], content_hash=content_hash, requested_keys=[],
                      granted_permissions=["presence"], requested_permissions=["presence"])
    atomic_write_json(data / "state" / "skills" / "slack-bridge" / AUTH_TOKEN_FILENAME,
                      {"token": TOKEN, "content_hash": content_hash, "issued_at": "fixture"})

    behavior = data / "skills" / "external" / "process-helper"
    behavior.mkdir(parents=True)
    (behavior / "SKILL.md").write_text(
        "---\nname: process-helper\ndescription: Status reports.\nversion: 0.1\ntype: instruction\n"
        "presence:\n  instructions: Prepare a complete status report.\n  capability_requests:\n"
        "    - id: history\n      kind: tool\n      required: true\n      purpose: Read conversation history.\n"
        "---\n# Fixture behavior\n")
    loaded = load_skill(behavior, data)
    assert loaded is not None
    save_enabled(data, loaded.name, True)
    save_review_state(data, loaded.name, SkillReviewState(status="pass", content_hash=loaded.content_hash))
    profile = parse_presence_profile(loaded.manifest, behavior)
    save_presence_state(data, loaded.name, PresenceState((PresenceSelection(
        presence_request_fingerprint(profile.capability_requests[0]), PresenceToolTarget("builtin", "chat_history"),
    ),)), expected_state_fingerprint=presence_state_fingerprint(PresenceState()))
    binding = PresenceBinding("d" * 32, "slack-bridge", loaded.name,
                              PresenceEndpoint("slack", "T1", "*", ""),
                              PresenceEndpoint("slack", "T1", "C1", "1.0"))
    return save_presence_binding(data, binding).binding_id


def main(root: Path, provider_url: str, mode: str, scenario: str = "main") -> None:
    import faulthandler
    faulthandler.dump_traceback_later(30, repeat=True)
    import httpx
    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from ouroboros import agent_task_pipeline as pipeline, loop, review_substrate
    from ouroboros.gateway.host_service import create_host_service_app
    from ouroboros.model_wait import task_model_wait_scope
    from ouroboros.presence_context import build_presence_context_section
    from ouroboros.presence_runner import run_presence_turn
    from ouroboros.review_execution import ReviewAttemptResult
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.tools.registry import ToolRegistry

    data, repo = root / "data", root / "repo"
    binding = seed_installation(data)
    current = threading.local()

    def admit_fixture_child(task, ctx=None):
        """The lifecycle harness supplies the confirmed supervisor-side handoff."""
        write_task_result(data, CHILD_REF, "scheduled", delegation_role="root", root_task_id=CHILD_REF,
                          description="Prepare an independent child report.",
                          metadata={"presence": dict(task["metadata"]["presence"])})
        if ctx is not None:
            ctx._swarm_handoff_attempt = {"status": "scheduled", "task_id": CHILD_REF}
        (root / "child-state.json").write_text(json.dumps({"work_ref": CHILD_REF, "turn_ref": task["id"]}))

    def inference(_llm, messages, *_args, **kwargs):
        if (scenario in {"late-child", "dead-child"}
                and "[PRESENCE CONVERSATION RESUMED]" in str(messages)
                and not getattr(current.ctx, "_swarm_handoff_attempt", None)):
            # Admission happens after the first real review reentry. The next
            # real park must retain this new ref without changing its initial.
            admit_fixture_child(current.task, current.ctx)
        observer = kwargs.get("model_context_observer")
        if callable(observer):
            observer(messages)
        with httpx.Client(trust_env=False, timeout=60) as client:
            reply = client.post(provider_url + "/model", json={
                "messages": messages, "source": current.source, "task_id": current.task_id,
                "pid": os.getpid(), "tools": [entry["function"]["name"] for entry in _args[1]],
            })
            reply.raise_for_status()
            return reply.json(), 0.0

    class LocalReviewExecutor:
        def __init__(self, assignment):
            self.assignment = assignment

        def restore_custody(self, _state):
            pass

        def set_pending_invocation_checkpoint(self, _checkpoint):
            pass

        def prompt_payload(self):
            return {"messages": []}

        def prompt_chars(self):
            return 0

        def failure_custody(self):
            return {}

        def execute(self):
            from ouroboros.review_dispatch import invoke_review_paid_stamp
            from ouroboros.review_evidence_refs import acceptance_evidence_ref_vocabulary

            invoke_review_paid_stamp(self.assignment.dispatch_stamp)
            vocabulary = acceptance_evidence_ref_vocabulary(self.assignment.request.evidence)
            ref = next(key for key, basis in vocabulary.items() if basis in {"tool_record", "packet_section"})
            with httpx.Client(trust_env=False, timeout=90) as client:
                response = client.post(provider_url + "/review", json={"ref": ref, "pid": os.getpid()})
                response.raise_for_status()
            text = json.dumps(response.json())
            return ReviewAttemptResult(message={"content": text}, raw_text=text,
                                       usage={"prompt_tokens": 5, "completion_tokens": 3,
                                              "physical_attempt_state": "settled"})

    # These are the only replaced callables: two external cognition transports.
    loop.call_llm_with_retry = inference
    review_substrate._review_route_executor = lambda assignment, **_kw: LocalReviewExecutor(assignment)

    class LoopAgent:
        def handle_task(self, task):
            if scenario == "refused-child":
                admit_fixture_child(task)
                write_task_result(data, task["id"], "failed",
                                  metadata={**task["metadata"], "presence_work_ref": CHILD_REF},
                                  reason_code="resource_refusal_no_resend", result="Private fixture diagnostic.")
                # run_presence_turn must refuse this unresolved original event
                # and retain the admitted child in its own PresenceTurnError.
                return [{"type": "presence_result", "outcome": "deferred", "text": "", "work_ref": CHILD_REF}]
            registry = ToolRegistry(repo_dir=repo, drive_root=data)
            ctx = registry._ctx
            ctx.is_direct_chat = True
            ctx.task_metadata = dict(task["metadata"])
            ctx.inline_max_rounds = int(task["metadata"]["inline_max_rounds"])
            source = task["metadata"]["presence"]["event"]["source_event_id"]
            reviewed = source.startswith("Review-")
            ctx.task_contract = {**task["task_contract"],
                                 "expected_output": "A complete status report." if reviewed else ""}
            write_task_result(data, task["id"], "running", task_contract=ctx.task_contract, root_task_id=task["id"])
            ctx.task_attempt, ctx.current_chat_id = 1, task["chat_id"]
            ctx.review_wait_callback = getattr(self, "review_wait_callback", None)
            current.source, current.task_id = source, task["id"]
            current.ctx, current.task = ctx, task
            task["_skip_post_task_synthesis"] = True
            messages = [{"role": "system", "content": "Presence turn.\n" +
                         build_presence_context_section(data, task["metadata"]["presence"], task["id"])},
                        {"role": "user", "content": task["text"]}]
            with task_model_wait_scope(task=task, drive_root=data, event_queue=None,
                                       worker_slot_held=False) as waiter:
                ctx.model_wait_context, waiter.tool_context = waiter, ctx
                text, usage, trace = loop.run_llm_loop(
                    messages, registry, SimpleNamespace(default_model=lambda: "fixture/main"), data / "logs",
                    lambda *_a, **_kw: None, queue.Queue(), task_type="presence", task_id=task["id"], drive_root=data)
            events = []
            pipeline.emit_task_results(SimpleNamespace(drive_root=data, repo_dir=repo), None, None, events,
                                       task, text, usage, trace, 0.0, data / "logs", ctx=ctx)
            return events

    def runner(**kwargs):
        return run_presence_turn(repo_dir=repo, drive_root=data, agent_factory=lambda **_kw: LoopAgent(), **kwargs)

    app = create_host_service_app(data, presence_runner=runner)

    async def ready(_request):
        return JSONResponse({"binding": binding, "pid": os.getpid(), "mode": mode})

    app.routes.append(Route("/fixture-ready", ready))
    async def sources(_request):
        import sys
        core = Path(os.environ["OUROBOROS_PRESENCE_CORE_ROOT"])
        used = sorted({str(Path(module.__file__).resolve().relative_to(core))
                       for module in sys.modules.values()
                       if getattr(module, "__file__", None) and Path(module.__file__).resolve().is_relative_to(core)})
        return JSONResponse(used)

    app.routes.append(Route("/fixture-sources", sources))

    async def child(request):
        state_path = root / "child-state.json"
        if not state_path.is_file():
            return JSONResponse({"ready": False})
        state = json.loads(state_path.read_text())
        if request.method == "POST":
            assert (await request.json()) == {"action": "complete"}
            write_task_result(data, state["work_ref"], "completed", result=CHILD_ANSWER,
                              terminal_origin="model_final",
                              metadata=load_task_result(data, state["work_ref"])["metadata"])
        parent = load_task_result(data, state["turn_ref"])
        return JSONResponse({"ready": True, **state, "parent": parent,
                             "child": load_task_result(data, state["work_ref"])})

    app.routes.append(Route("/fixture-child", child, methods=["GET", "POST"]))
    async def shutdown(_request):
        server.should_exit = True
        return JSONResponse({"stopping": True})

    app.routes.append(Route("/fixture-shutdown", shutdown, methods=["POST"]))
    # Bind once and hand the same socket to uvicorn; no port selection race.
    import socket
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    (root / "host-address.json").write_text(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}"}))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    server.run(sockets=[listener])


if __name__ == "__main__":
    import sys
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "main")
