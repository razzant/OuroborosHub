"""Reaction doors of slack-bridge and telegram-bot as a real core loads, offers, guards and dispatches them (local opt-in).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_gesture_core_consumer.py

Without that variable this is reported as skipped, never as passed. Both payloads are copied, reviewed,
enabled and granted as an install is, then loaded by core's own ``reload_all`` in a subprocess whose
HOME, data and settings are temporary. Core's ``effect_of``, its door listing and ``speak_line``,
its call-time place guard and its real extension dispatch are exercised; only the LLM safety check is
stubbed and nothing reaches Slack, Telegram or a live Host. Slack's declared packages are recorded as
installed with none of their own (``_deps_from_this_interpreter``): they import from the test interpreter. The same assertions describe both kinds of
core: one whose ``tool_effects`` knows ``gesture`` offers the reaction doors with the waking message's
id filled in, and one that predates it reads the unknown effect as an undeclared write and offers no
reaction door. Run it once against each to see the pair.
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
SKILLS = ("slack-bridge", "telegram-bot")
GESTURES = {"slack-bridge": ["slack_reaction_add", "slack_reaction_remove"], "telegram-bot": ["telegram_react"]}
REFUSED = "⚠️ RESOURCE_CONSTRAINT_BLOCKED"


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; no core loaded these doors")
    tmp_path = tmp_path_factory.mktemp("gesture-core")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": str(Path(core).resolve()), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),  # never inside the core checkout
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run([sys.executable, __file__, str(tmp_path)], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_both_skills_load_through_the_real_loader_with_their_tools(facts):
    assert facts["errors"] == {}
    for skill, names in GESTURES.items():
        assert set(names) <= set(facts["registered"][skill])
    assert {"telegram_operation", "telegram_send"} <= set(facts["registered"]["telegram-bot"])


def test_each_core_reads_the_declaration_it_understands(facts):
    effects = facts["effects"]
    if facts["knows_gesture"]:
        for skill, names in GESTURES.items():
            for name in names:
                assert effects[name]["effect"] == "gesture" and effects[name]["declared"] is True, name
        assert effects["slack_reaction_add"]["message"] == "ts" and effects["slack_reaction_add"]["conversation"] == "channel"
        assert effects["telegram_react"]["message"] == "message_id" and effects["telegram_react"]["thread"] == "topic_id"
    else:
        # An older core does not know the word: an undeclared write, never a read or a message.
        for names in GESTURES.values():
            for name in names:
                assert effects[name]["effect"] == "write" and effects[name]["declared"] is False, name
    # The mixed tool stays a declared write on every core; the send doors stay messages.
    assert effects["telegram_operation"] == {**effects["telegram_operation"], "effect": "write", "declared": True}
    assert effects["telegram_send"]["effect"] == effects["slack_send"]["effect"] == "message"


def test_only_a_core_that_knows_gestures_offers_the_reaction_door(facts):
    speak = facts["speak"]
    if speak is None:  # a core that lists no doors at all, such as the pinned CI core: none to offer
        assert not facts["knows_gesture"] and facts["gesture_doors"] is None and facts["send_doors"] is None
        return
    if facts["knows_gesture"]:
        assert facts["gesture_doors"] == GESTURES
        assert "gesture: ext_" in speak["slack-bridge"] and "channel=C1, ts=1712.5)" in speak["slack-bridge"]
        assert "chat_id=55, message_id=7)" in speak["telegram-bot"]
        for line in speak.values():
            assert "words: ext_" in line and "is not speech" in line
    else:
        assert facts["gesture_doors"] is None
        for line in speak.values():  # its own older wording of the word doors, and no reaction door
            assert "gesture" not in line and "_send (" in line
            assert not any(name in line for names in GESTURES.values() for name in names)
    # The word doors are unchanged, and no reaction tool is counted among them.
    assert facts["send_doors"] == {"slack-bridge": ["slack_file_upload", "slack_message_edit", "slack_send"],
                                   "telegram-bot": ["telegram_send"]}


def test_the_place_guard_holds_every_reaction_door_outside_an_active_acting_place(facts):
    guards = facts["guards"]
    for name in ("slack_reaction_add", "telegram_react", "telegram_operation"):
        assert guards["active"][name] == "", name
        for situation in ("advises", "silent", "paused"):
            assert guards[situation][name].startswith(REFUSED), (situation, name)


def test_telegram_react_queues_exactly_what_telegram_operation_queues(facts):
    react = facts["telegram"]
    assert react["react_set"] == {"ok": True, "state": "queued", "request_id": "react-set", "method": "setMessageReaction",
                                  "chat_id": "55", "message_id": 7}
    assert react["payload_react"] == react["payload_operation"]
    assert react["payload_react"]["parameters"] == {"chat_id": "55", "message_id": 7,
                                                    "reaction": [{"type": "emoji", "emoji": "👍"}], "is_big": False}
    assert react["payload_clear"]["parameters"]["reaction"] == []
    # One request_id is one operation whichever tool queued it, and no new receipt kind appears.
    assert react["dedup"]["state"] == "already_queued"
    assert react["receipt"]["state"] in {"pending", "leased"} and react["delivery_ids"] == [
        "telegram-operation:op-set", "telegram-operation:react-clear", "telegram-operation:react-set"]
    assert react["refused"]["ok"] is False and react["refused_queued"] is False


def test_slack_reaction_door_dispatches_into_the_existing_queue(facts):
    slack = facts["slack"]
    assert slack["answer"]["ok"] is True and slack["answer"]["operation"] == "reaction_add"
    assert slack["again"]["deduplicated"] is True


# -- The subprocess -----------------------------------------------------------------------------

def _install(root):
    import shutil

    from ouroboros import extension_loader
    from ouroboros.config import load_settings
    from ouroboros.skill_loader import SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants

    os.environ["OUROBOROS_RUNTIME_MODE"] = "advanced"
    os.environ["OUROBOROS_HOST_SERVICE_PORT"] = "1"  # never a live Host's port
    data, skills = root / "data", root / "skills"
    keys = {"telegram-bot": ["TELEGRAM_PUBLIC_BOT_TOKEN"], "slack-bridge": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]}
    Path(os.environ["OUROBOROS_SETTINGS_PATH"]).write_text(json.dumps({
        "TELEGRAM_PUBLIC_BOT_TOKEN": "fixture-token", "SLACK_BOT_TOKEN": "xoxb-fixture", "SLACK_APP_TOKEN": "xapp-fixture"}),
        encoding="utf-8")
    for skill in SKILLS:
        shutil.copytree(ROOT / "skills" / skill, skills / skill,
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".DS_Store"))
        loaded = find_skill(data, skill, repo_path=str(skills))
        save_enabled(data, skill, True)
        save_review_state(data, skill, SkillReviewState(status="pass", content_hash=loaded.content_hash))
        save_skill_grants(data, skill, keys[skill], content_hash=loaded.content_hash, requested_keys=keys[skill],
                          granted_permissions=["presence"], requested_permissions=["presence"])
        _deps_from_this_interpreter(data, find_skill(data, skill, repo_path=str(skills)))
    errors = extension_loader.reload_all(data, load_settings, repo_path=str(skills))
    return data, {skill: str(error) for skill, error in errors.items() if error}


def _deps_from_this_interpreter(data, skill):
    """Record a declared dependency set as installed, as core's installer would, with no packages of its own.

    Core prepares a skill's packages when the owner enables it; this test home cannot, so the record says
    installed with an empty package list and the imports resolve from the test interpreter (which has them).
    """
    from ouroboros.marketplace.install_specs import install_specs_hash
    from ouroboros.marketplace.isolated_deps import DEPS_STATE_FILENAME, FINGERPRINT_FILENAME, isolated_env_dir
    from ouroboros.skill_dependencies import auto_install_specs_for_skill
    from ouroboros.skill_loader import skill_state_dir
    from ouroboros.utils import atomic_write_json

    specs = auto_install_specs_for_skill(data, skill)
    if not specs:
        return
    record = {"schema_version": 1, "skill": skill.name, "specs_hash": install_specs_hash(specs), "installed": [],
              "status": "installed", "error": ""}
    isolated_env_dir(skill.skill_dir).mkdir(parents=True, exist_ok=True)
    atomic_write_json(isolated_env_dir(skill.skill_dir) / FINGERPRINT_FILENAME, record)
    atomic_write_json(skill_state_dir(data, skill.name) / DEPS_STATE_FILENAME, record)


def _metadata(connector, account, conversation, message, *, state="active"):
    return {"wake_cause": "focus", "presence": {
        "transport_skill": connector, "place": {"label": "chat", "state": state},
        "event": {"account_id": account, "conversation_id": conversation, "thread_id": "",
                  "message": {"message_id": message}}}}


def _drive(root):
    import ast
    import sqlite3
    from types import SimpleNamespace

    from ouroboros import extension_loader, extension_registry_state as registry, place_guard, place_reply, places as p
    from ouroboros import safety, tool_effects
    from ouroboros.extension_surface_names import extension_surface_name, parse_extension_surface_name
    from ouroboros.tools.extension_dispatch import _dispatch_extension_tool_result

    def short(surface):
        return parse_extension_surface_name(surface)[1]

    safety.check_safety = lambda *_args, **_kwargs: (True, "")
    data, errors = _install(root)
    names = {"slack-bridge": ("slack_reaction_add", "slack_reaction_remove", "slack_send"),
             "telegram-bot": ("telegram_react", "telegram_operation", "telegram_send", "telegram_receipt")}
    full = {skill: {name: extension_surface_name(skill, name) for name in names[skill]} for skill in SKILLS}
    with registry._lock:
        registered = {skill: sorted(short(surface) for surface, tool in registry._tools.items()
                                    if isinstance(tool, dict) and tool.get("skill") == skill) for skill in SKILLS}

    effects = {}
    for skill in SKILLS:
        for name, surface in full[skill].items():
            effect = tool_effects.effect_of(surface, ext_tool=extension_loader.get_tool(surface))
            effects[name] = {"effect": effect.effect, "declared": effect.declared,
                             "conversation": effect.conversation, "thread": effect.thread,
                             "message": getattr(effect, "message", None)}
    knows = hasattr(tool_effects, "EFFECT_GESTURE")
    gesture_doors = ({skill: sorted(short(name) for name, _effect in place_reply.conversation_gesture_tools(skill))
                      for skill in SKILLS} if hasattr(place_reply, "conversation_gesture_tools") else None)
    lists_doors = hasattr(place_reply, "conversation_send_tools") and hasattr(place_reply, "speak_line")
    send_doors = ({skill: sorted(short(name) for name, _effect in place_reply.conversation_send_tools(skill))
                   for skill in SKILLS} if lists_doors else None)
    speak = ({"slack-bridge": place_reply.speak_line(_metadata("slack-bridge", "T1", "C1", "1712.5")),
              "telegram-bot": place_reply.speak_line(_metadata("telegram-bot", "9", "55", 7))} if lists_doors else None)

    slack_place = p.connect_place(data, connector="slack-bridge", provider="slack", account_id="T1", conversation_id="C1",
                                  wakes=["any_message"])
    telegram_place = p.connect_place(data, connector="telegram-bot", provider="telegram", account_id="9",
                                     conversation_id="55", wakes=["any_message"])
    calls = {"slack_reaction_add": ("slack-bridge", {"channel": "C1", "ts": "1712.5", "name": "eyes"}),
             "telegram_react": ("telegram-bot", {"action": "set", "chat_id": "55", "message_id": 7, "emoji": "👍"}),
             "telegram_operation": ("telegram-bot", {"method": "setMessageReaction", "chat_id": "55", "message_id": 7,
                                                     "reaction": [{"type": "emoji", "emoji": "👍"}]})}

    def guard(metadata):
        ctx = SimpleNamespace(drive_root=data, task_id="caller", task_metadata=metadata)
        return {name: place_guard.dispatch_refusal(ctx, full[skill][name], {**args, "request_id": "g"},
                                                   ext_tool=extension_loader.get_tool(full[skill][name]))
                for name, (skill, args) in calls.items()}

    guards = {}
    for state in ("silent", "paused", "active"):
        for place in (slack_place, telegram_place):
            p.edit_place(data, place.place_id, "set_state", author="owner", state=state)
        guards[state] = guard({})
    guards["advises"] = guard({"initiator": "consciousness", "consciousness_autonomy": "observe"})

    from ouroboros.tools.tool_context import ToolContext
    import ouroboros
    # The extension child runs ``-m ouroboros.extension_process_runner`` from the core checkout.
    ctx = ToolContext(repo_dir=Path(ouroboros.__file__).resolve().parents[1], drive_root=data, task_id="caller")

    def dispatch(skill, name, args):
        surface = full[skill][name]
        result = _dispatch_extension_tool_result(ctx, surface, extension_loader.get_tool(surface), args)
        try:
            return json.loads(result.text)
        except ValueError:
            return ast.literal_eval(result.text)

    telegram = {"react_set": dispatch("telegram-bot", "telegram_react", {**calls["telegram_react"][1], "request_id": "react-set"})}
    dispatch("telegram-bot", "telegram_operation", {**calls["telegram_operation"][1], "request_id": "op-set"})
    dispatch("telegram-bot", "telegram_react", {"action": "clear", "chat_id": "55", "message_id": 7, "request_id": "react-clear"})
    telegram["dedup"] = dispatch("telegram-bot", "telegram_operation", {**calls["telegram_operation"][1], "request_id": "react-set",
                                                                        "reaction": [{"type": "emoji", "emoji": "❤"}]})
    telegram["refused"] = dispatch("telegram-bot", "telegram_react", {"action": "set", "chat_id": "55", "message_id": 7,
                                                                      "request_id": "react-none"})
    telegram["receipt"] = dispatch("telegram-bot", "telegram_receipt", {"operation": "operation", "request_id": "react-set"})
    with sqlite3.connect(data / "state" / "skills" / "telegram-bot" / "custody.sqlite3"
                         if (data / "state" / "skills" / "telegram-bot" / "custody.sqlite3").exists()
                         else next(data.rglob("custody.sqlite3"))) as db:
        rows = dict(db.execute("SELECT delivery_id, payload_json FROM outbox").fetchall())
    payloads = {key: json.loads(value) for key, value in rows.items()}
    telegram.update(delivery_ids=sorted(payloads), payload_react=payloads.get("telegram-operation:react-set"),
                    payload_operation=payloads.get("telegram-operation:op-set"),
                    payload_clear=payloads.get("telegram-operation:react-clear"),
                    refused_queued="telegram-operation:react-none" in payloads)
    slack_args = {**calls["slack_reaction_add"][1], "request_id": "slack-r"}
    slack = {"answer": dispatch("slack-bridge", "slack_reaction_add", slack_args),
             "again": dispatch("slack-bridge", "slack_reaction_add", slack_args)}
    return {"errors": errors, "registered": registered, "effects": effects, "knows_gesture": knows,
            "gesture_doors": gesture_doors, "send_doors": send_doors, "speak": speak, "guards": guards,
            "telegram": telegram, "slack": slack}


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1])), ensure_ascii=False, default=str))
