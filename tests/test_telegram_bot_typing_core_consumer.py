"""telegram_typing as a real core loads, guards and dispatches it inside its own Presence turns (local opt-in; CI selects it).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_telegram_bot_typing_core_consumer.py

Without that variable this is reported as skipped, never as passed. The payload is copied, reviewed, enabled and granted
as an install is and loaded by core's own ``reload_all`` (PluginAPI) in a subprocess whose HOME, data and settings are
temporary. Each turn is admitted by core's Host Service app over ASGI from the plugin's own runtime and built by core's
Presence runner. A stand-in agent builds the turn's ToolContext from that task as core's agent does (``task_id`` and
``task_metadata``) and calls tools through core's ToolRegistry: argument bindings, place guard and extension dispatch. The
plugin's runtime then shows "typing…", reading the place through the same Host app. Only skill-token authentication, the
focus allowance and the LLM safety check are stubbed; Telegram's HTTP answers are scripted and nothing leaves the machine.
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
BOT, CHAT, TOPIC = "9", "-100500", 77
REFUSED = "⚠️ RESOURCE_CONSTRAINT_BLOCKED"


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; no core dispatched telegram_typing")
    tmp_path = tmp_path_factory.mktemp("typing-core")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": str(Path(core).resolve()), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),  # never inside the core checkout
    }
    for name in ("home", "app", "data", "repo", "skills"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run([sys.executable, __file__, str(tmp_path)], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_core_reads_the_door_as_a_conversation_write_and_hands_the_turn_to_it(facts):
    assert facts["errors"] == {} and facts["in_process"] is True
    assert facts["effect"] == {"effect": "write", "declared": True, "conversation": "chat_id", "thread": "topic_id"}
    turn = facts["turns"]["asks"]
    # Core's own task carries the event the tool reads, and the task id is the turn_ref the Host answered the transport.
    assert turn["ctx"]["provider"] == "telegram" and turn["ctx"]["source_event_id"] == f"telegram:{BOT}:1"
    assert (turn["ctx"]["conversation_id"], turn["ctx"]["thread_id"]) == (CHAT, str(TOPIC))
    assert turn["ctx"]["task_id"].startswith("presence-") and turn["turn_ref"] == turn["ctx"]["task_id"]


def test_the_tool_answers_requested_and_the_runtime_shows_it_only_in_that_turns_topic(facts):
    turn = facts["turns"]["asks"]
    assert turn["before_asking"] == 0  # no automatic "typing…" at the start of a turn
    assert turn["answer"]["ok"] is True and turn["answer"]["typing"] == "requested"
    assert (turn["answer"]["chat_id"], turn["answer"]["topic_id"]) == (CHAT, TOPIC)
    assert turn["actions_at_answer"] == 0  # the call itself contacted nobody
    assert turn["requests"] == [[CHAT, str(TOPIC), turn["ctx"]["task_id"], f"telegram:{BOT}:1"]]
    assert turn["shown"] == [1, 1] and turn["place_reads"] >= 2  # the place is read again for the renewal
    assert facts["actions"][:2] == [{"chat_id": CHAT, "action": "typing", "message_thread_id": TOPIC}] * 2
    other = turn["other_conversation"]
    assert other["ok"] is False and other["code"] == "other_conversation"


def test_the_first_message_there_ends_it_and_the_final_reply_is_still_delivered(facts):
    turn = facts["turns"]["asks"]
    assert turn["ack"]["ok"] is True and turn["after_ack"] == {"shown": 0, "requests": 0}
    assert turn["asked_again"]["typing"] == "requested" and turn["shown_again"] == 1
    # Typing is no speech: the turn's own words still reach Telegram as its reply, in its topic.
    assert turn["final"] == {"text": "Final answer", "message_thread_id": TOPIC}


def test_silent_and_paused_stop_it_and_core_refuses_new_requests_there(facts):
    turn = facts["turns"]["asks"]
    assert turn["after_pause"] == {"shown": 0, "requests": 0}
    assert turn["asked_while_paused"]["status"] == "blocked" and turn["asked_while_paused"]["text"].startswith(REFUSED)
    assert turn["requests_while_paused"] == 0
    silent = facts["turns"]["silent_place"]
    assert silent["answer"]["status"] == "blocked" and silent["requests"] == 0 and silent["shown"] == 0


def test_a_turn_that_does_not_ask_shows_nothing(facts):
    quiet = facts["turns"]["quiet"]
    assert quiet == {"shown": 0, "requests": 0, "actions": 0}


@pytest.mark.parametrize("name", ["reply_while_reading", "end_while_reading"])
def test_an_end_met_while_the_place_is_read_sends_nothing(facts, name):
    turn = facts["turns"][name]
    assert turn["answer"]["typing"] == "requested" and turn["ran_meanwhile"] is True
    assert turn["shown"] == 0 and turn["requests"] == 0 and turn["actions"] == 0


def test_a_429_ends_it_unretried_and_two_minutes_end_it(facts):
    turn = facts["turns"]["limits"]
    assert turn["after_429"] == {"shown": [0, 0], "actions": 1, "requests": 0}
    assert turn["capped"] == {"shown": [1, 0], "requests": 0}


def test_a_restart_forgets_requests_even_of_a_live_turn(facts):
    turn = facts["turns"]["restart"]
    assert turn["live_before"] is True and turn["requests_before"] == 1
    # The restarted runtime submitted the event again and core joined its still running turn; nothing was shown.
    assert turn["live_after"] is True and turn["requests_after"] == 0
    assert turn["shown_after"] == 0 and turn["actions"] == 0


# -- The subprocess -----------------------------------------------------------------------------


def _install(root):
    import shutil

    from ouroboros import extension_loader
    from ouroboros.config import load_settings
    from ouroboros.skill_loader import SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants

    os.environ["OUROBOROS_RUNTIME_MODE"] = "advanced"
    os.environ["OUROBOROS_HOST_SERVICE_PORT"] = "1"  # never a live Host's port
    data, skills, keys = root / "data", root / "skills", ["TELEGRAM_PUBLIC_BOT_TOKEN"]
    Path(os.environ["OUROBOROS_SETTINGS_PATH"]).write_text(json.dumps({keys[0]: "fixture-token"}), encoding="utf-8")
    shutil.copytree(ROOT / "skills" / "telegram-bot", skills / "telegram-bot",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".DS_Store"))
    loaded = find_skill(data, "telegram-bot", repo_path=str(skills))
    save_enabled(data, "telegram-bot", True)
    save_review_state(data, "telegram-bot", SkillReviewState(status="pass", content_hash=loaded.content_hash))
    save_skill_grants(data, "telegram-bot", keys, content_hash=loaded.content_hash, requested_keys=keys,
                      granted_permissions=["presence"], requested_permissions=["presence"])
    errors = extension_loader.reload_all(data, load_settings, repo_path=str(skills))
    return data, {skill: str(error) for skill, error in errors.items() if error}


def _drive(root):
    import ast
    import asyncio
    import queue
    import time
    import types
    from urllib.parse import urlsplit

    import httpx

    from ouroboros import extension_loader, places as p, presence_admission, presence_context, safety, tool_effects
    from ouroboros.extension_surface_names import extension_surface_name
    from ouroboros.gateway import host_service
    from ouroboros.presence_runner import run_presence_turn
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    safety.check_safety = lambda *_args, **_kwargs: (True, "")  # the LLM supervisor is not under test
    data, errors = _install(root)
    surface = {name: extension_surface_name("telegram-bot", name) for name in ("telegram_typing", "telegram_send")}
    door = extension_loader.get_tool(surface["telegram_typing"])
    effect = tool_effects.effect_of(surface["telegram_typing"], ext_tool=door)
    # The plugin's own module objects as core loaded them: its runtime, store and clients.
    plugin = next(module for module in list(sys.modules.values()) if getattr(module, "_RUNTIME", None) is not None)
    runtime = plugin._RUNTIME
    package = type(runtime).__module__.rsplit(".", 1)[0]
    modules = {name: sys.modules[f"{package}.{name}"] for name in ("runtime", "custody", "events", "api", "host")}
    offset = [0.0]  # a wall clock moved forward for the plugin only; core keeps the real one
    clock = types.SimpleNamespace(time=lambda: time.time() + offset[0], strftime=time.strftime, gmtime=time.gmtime,
                                  sleep=time.sleep)
    modules["runtime"].time = modules["custody"].time = clock

    place = p.connect_place(data, connector="telegram-bot", provider="telegram", account_id=BOT, conversation_id=CHAT,
                            wakes=["any_message"])
    p.edit_place(data, place.place_id, "set_state", author="owner", state="active")
    presence_admission.focus_enabled = lambda _root: True
    presence_admission.focus_allowance = lambda _root: {"status": "available", "remaining_usd": 2, "settled_usd": 1}
    presence_context._own_work_section = lambda *_args: ""

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token  # the token core issued to this skill
        return "telegram-bot", {}
    host_service._authenticated = authenticated

    registry = ToolRegistry(repo_dir=root / "repo", drive_root=data)
    commands, answers, started = queue.Queue(), queue.Queue(), queue.Queue()

    class Agent:
        """Builds the turn's ToolContext as core's agent does and runs the driver's tool calls through core's registry."""

        def handle_task(self, task):
            metadata = dict(task.get("metadata") or {})
            contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
            ctx = ToolContext(repo_dir=root / "repo", drive_root=data, task_id=str(task.get("id") or ""),
                              task_metadata=metadata, task_contract=contract)
            event = metadata["presence"]["event"]
            started.put({"task_id": ctx.task_id, **{key: event.get(key) for key in (
                "provider", "source_event_id", "conversation_id", "thread_id")}})
            while True:
                name, args = commands.get(timeout=120)
                if name == "finish":
                    break
                result = registry.copy_for(ctx).execute_result(surface[name], args)
                answers.put({"status": result.status, "text": result.text})
            write_task_result(data, task["id"], "completed", metadata=task["metadata"], result=args.get("text", ""))
            return [{"type": "presence_result", "outcome": args["outcome"], "text": args.get("text", ""), "work_ref": ""}]

    def runner(**kwargs):
        return run_presence_turn(repo_dir=root / "repo", drive_root=data, agent_factory=lambda **_: Agent(), **kwargs)

    asgi = httpx.ASGITransport(app=host_service.create_host_service_app(data, presence_runner=runner))

    class HostTransport:
        """The plugin's Host client reaches core's app over ASGI; ``meanwhile`` runs once during a place read."""
        place_reads, meanwhile = 0, None

        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            parts = urlsplit(url)
            target = parts.path + (f"?{parts.query}" if parts.query else "")
            if parts.path == "/presence/place":
                self.place_reads += 1
                act, self.meanwhile = self.meanwhile, None
                if act is not None:
                    await act()
            async with httpx.AsyncClient(transport=asgi, base_url="http://host", timeout=60.0) as client:
                response = await client.request(method, target, headers=headers, json=payload)
            body = response.json()
            if response.status_code >= 400:
                raise modules["host"].PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    class Telegram:
        """Telegram's Bot API as scripted answers behind the plugin's own TelegramClient."""

        def __init__(self):
            self.calls, self.answers, self.polling, self.polled = [], {}, None, False

        async def post_json(self, url, payload, timeout_sec):
            method = url.rsplit("/", 1)[-1]
            if method == "getUpdates" and self.polling is not None:
                self.polled = True
                await self.polling.wait()  # a long poll that ends when the test says
            self.calls.append((method, dict(payload)))
            if self.answers.get(method):
                return self.answers[method].pop(0)
            if method == "getMe":
                return {"ok": True, "result": {"id": int(BOT), "is_bot": True, "username": "presence_bot"}}
            if method in {"deleteWebhook", "sendChatAction"}:
                return {"ok": True, "result": True}
            if method == "getUpdates":
                return {"ok": True, "result": []}
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 900 + len(self.calls), "date": 1_800_000_000,
                                               "chat": {"id": int(payload["chat_id"]), "type": "supergroup",
                                                        "is_forum": True}, "text": payload.get("text", "")}}
            raise AssertionError(f"unscripted Bot API method {method}")

    host, telegram = HostTransport(), Telegram()
    runtime.submitter.transport = host
    runtime.client_factory = lambda token: modules["api"].TelegramClient(token, transport=telegram)

    def actions():
        return [fields for method, fields in telegram.calls if method == "sendChatAction"]

    def requests():
        return [[row["chat_id"], row["topic_id"], row["turn_ref"], row["source_event_id"]]
                for row in runtime.store.typing_requests()]

    def parsed(answer):
        text = answer["text"]
        try:
            value = json.loads(text)
        except ValueError:
            try:
                value = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return answer
        return value if isinstance(value, dict) else answer

    async def call(name, **args):
        commands.put((name, args))
        return await asyncio.to_thread(answers.get, True, 60)

    async def drain(method):
        while await getattr(runtime, method)():
            pass

    async def begin(number, chat=CHAT, topic=TOPIC):
        message = {"message_id": 10 + number, "date": 1_700_000_000 + number, "text": f"question {number}",
                   "chat": {"id": int(chat), "type": "supergroup", "title": "Lab", "is_forum": True},
                   "from": {"id": 55, "is_bot": False, "first_name": "Ann"}}
        if topic:
            message.update(message_thread_id=topic, is_topic_message=True)
        event = modules["events"].parse_telegram_update({"update_id": number, "message": message},
                                                        bot_account_id=BOT, places=True)
        runtime.store.commit_update(number, event)
        task = asyncio.create_task(runtime.process_one_inbox())
        ctx = await asyncio.to_thread(started.get, True, 60)
        return task, ctx

    async def finish(turn, outcome="silent", text=""):
        commands.put(("finish", {"outcome": outcome, "text": text}))
        await turn.request
        for _ in range(200):  # a continuation stays observed until its terminal reaches the transport
            await drain("process_one_work")
            if not runtime.store.turn_live(turn.source, turn.ref):
                break
            await asyncio.sleep(0.05)
        await drain("process_one_outbox")

    async def tracked(number, **kwargs):
        request, ctx = await begin(number, **kwargs)
        return types.SimpleNamespace(request=request, source=f"telegram:{BOT}:{number}", ref=ctx["task_id"]), ctx

    async def scenario():
        await runtime.submitter.discover_delivery_support()
        turns = {}

        # 1. A turn in forum topic 77 asks, renews, sends an intermediate message, asks again, is paused, then answers.
        task, ctx = await tracked(1)
        one = turns["asks"] = {"ctx": ctx, "before_asking": await runtime.process_typing()}
        one["answer"] = parsed(await call("telegram_typing", chat_id=CHAT, topic_id=TOPIC))
        one["actions_at_answer"], one["requests"] = len(actions()), requests()
        one["other_conversation"] = parsed(await call("telegram_typing", chat_id="-100999"))
        first = await runtime.process_typing()
        offset[0] += 5
        one["shown"], one["place_reads"] = [first, await runtime.process_typing()], host.place_reads
        one["ack"] = parsed(await call("telegram_send", chat_id=CHAT, topic_id=str(TOPIC), text="On it",
                                       request_id="ack"))
        await drain("process_one_outbox")
        offset[0] += 5
        one["after_ack"] = {"shown": await runtime.process_typing(), "requests": len(requests())}
        one["asked_again"] = parsed(await call("telegram_typing", chat_id=CHAT, topic_id=TOPIC))
        one["shown_again"] = await runtime.process_typing()
        p.edit_place(data, place.place_id, "set_state", author="owner", state="paused")
        offset[0] += 5
        one["after_pause"] = {"shown": await runtime.process_typing(), "requests": len(requests())}
        one["asked_while_paused"] = await call("telegram_typing", chat_id=CHAT, topic_id=TOPIC)
        one["requests_while_paused"] = len(requests())
        p.edit_place(data, place.place_id, "set_state", author="owner", state="active")
        await finish(task, "message", "Final answer")
        one["turn_ref"] = _submission_ref(runtime, f"telegram:{BOT}:1")
        sent = [fields for method, fields in telegram.calls if method == "sendMessage"]
        one["final"] = {"text": sent[-1]["text"], "message_thread_id": sent[-1].get("message_thread_id")}

        # 2. A turn that never asks: nothing is shown however often the runtime looks.
        before = len(actions())
        task, ctx = await tracked(2)
        shown = [await runtime.process_typing() for _ in range(2)]
        turns["quiet"] = {"shown": sum(shown), "requests": len(requests()), "actions": len(actions()) - before}
        await finish(task)

        # 3. A reply delivered while the runtime reads the place: the call it was about to make is not made.
        before = len(actions())
        task, ctx = await tracked(3)
        three = turns["reply_while_reading"] = {"answer": parsed(await call("telegram_typing", chat_id=CHAT,
                                                                             topic_id=TOPIC)), "ran_meanwhile": False}
        await call("telegram_send", chat_id=CHAT, topic_id=str(TOPIC), text="Partial", request_id="partial")

        async def deliver():
            three["ran_meanwhile"] = True
            await drain("process_one_outbox")
        host.meanwhile, offset[0] = deliver, offset[0] + 5  # a showing is due
        three.update(shown=await runtime.process_typing(), requests=len(requests()), actions=len(actions()) - before)
        if host.meanwhile is not None:  # no place read happened; the assertion says so, and the run goes on
            host.meanwhile = None
            await drain("process_one_outbox")
        await finish(task)

        # 4. The turn ends while the runtime reads the place.
        before = len(actions())
        task, ctx = await tracked(4)
        four = turns["end_while_reading"] = {"answer": parsed(await call("telegram_typing", chat_id=CHAT,
                                                                          topic_id=TOPIC)), "ran_meanwhile": False}

        async def end():
            four["ran_meanwhile"] = True
            await finish(task)
        host.meanwhile, offset[0] = end, offset[0] + 5
        four.update(shown=await runtime.process_typing(), requests=len(requests()), actions=len(actions()) - before)
        if host.meanwhile is not None:
            host.meanwhile = None
            await finish(task)

        # 5. Telegram's 429 ends a request unretried; a new request ends after two minutes.
        task, ctx = await tracked(5)
        five = turns["limits"] = {}
        await call("telegram_typing", chat_id=CHAT, topic_id=TOPIC)
        before = len(actions())
        telegram.answers["sendChatAction"] = [{"ok": False, "error_code": 429, "parameters": {"retry_after": 3},
                                               "description": "Too Many Requests: retry after 3"}]
        refused = await runtime.process_typing()
        offset[0] += 10
        five["after_429"] = {"shown": [refused, await runtime.process_typing()], "actions": len(actions()) - before,
                             "requests": len(requests())}
        await call("telegram_typing", chat_id=CHAT, topic_id=TOPIC)
        shown = await runtime.process_typing()
        offset[0] += 121
        five["capped"] = {"shown": [shown, await runtime.process_typing()], "requests": len(requests())}

        # 6. The transport restarts while core's turn runs on: its open request dies with the old process, and the
        # plugin's runtime starts again (run()). The request is gone before any worker could show it.
        await call("telegram_typing", chat_id=CHAT, topic_id=TOPIC)
        six = turns["restart"] = {"requests_before": len(requests()),
                                  "live_before": runtime.store.turn_live(task.source, task.ref)}
        before = len(actions())
        task.request.cancel()  # the old process's request, as a process exit ends it
        await asyncio.gather(task.request, return_exceptions=True)
        telegram.polling = asyncio.Event()
        running = asyncio.ensure_future(runtime.run())
        for _ in range(400):  # the restarted inbox worker submits the event again; core joins its live turn
            await asyncio.sleep(0.05)
            if runtime.store.turn_live(task.source, task.ref) and telegram.polled:
                break
        six.update(requests_after=len(requests()), live_after=runtime.store.turn_live(task.source, task.ref),
                   shown_after=await runtime.process_typing(), actions=len(actions()) - before)
        runtime.stop()
        telegram.polling.set()
        await asyncio.gather(running, return_exceptions=True)
        commands.put(("finish", {"outcome": "silent", "text": ""}))
        for _ in range(200):  # the stored answer, replayed to a new request once core's turn has ended
            if await runtime.process_one_inbox() and not runtime.store.turn_live(task.source, task.ref):
                break
            await asyncio.sleep(0.05)

        # 7. A Silent place: core's place guard refuses the request, so nothing is recorded or shown.
        p.edit_place(data, place.place_id, "set_state", author="owner", state="silent")
        task, ctx = await tracked(6)
        seven = turns["silent_place"] = {"answer": await call("telegram_typing", chat_id=CHAT, topic_id=TOPIC)}
        seven.update(requests=len(requests()), shown=await runtime.process_typing())
        await finish(task)
        return turns

    turns = asyncio.run(scenario())
    return {"errors": errors, "in_process": not door.get("out_of_process"),
            "effect": {"effect": effect.effect, "declared": effect.declared, "conversation": effect.conversation,
                       "thread": effect.thread},
            "turns": turns, "actions": actions()}


def _submission_ref(runtime, event_id):
    """The turn_ref the Host answered for this event, as custody stored it."""
    import sqlite3

    with sqlite3.connect(runtime.state_dir / "custody.sqlite3") as conn:
        row = conn.execute("SELECT submission_ref FROM inbox WHERE event_id=?", (event_id,)).fetchone()
    return row[0] if row else ""


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1])), ensure_ascii=False, default=str))
