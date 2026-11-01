"""Slack and Telegram reaction facts through a real core's Presence routes (local opt-in).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_reaction_core_consumer.py

Without that variable, or with a core lacking the reaction fact, this is reported as skipped,
never as passed. The connectors run unchanged (parser, store, workers, provider client, Host
client); providers are scripted and core's own app answers over ASGI. Only skill-token
authentication and inference are stubbed. The connectors read ``/identity`` as one of three Hosts,
whatever a core advertises today. ``OLD_HOST`` strips both capabilities. ``EXPLICIT_ONLY_HOST``
strips the reaction context and leaves core's own answer on explicit completion, so its positive
case is real. ``CAPABLE_HOST`` adds both as a labelled stand-in: it proves the schema and route
seam, not that a core is ready to advertise them, and not the sleeping-author or final-selection
paths. The worker tests load the skill through core's real loader in separate processes and call it
through the real dispatch; only the LLM safety check and what ``urllib`` would reach (Telegram, and
the Host answering the polling process) are stubbed. Every run is a subprocess whose HOME, data and
settings are temporary.
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
# What each Host's /identity says about reactions, over core's own answer.
OLD_HOST = {"drop": ("presence_reaction_context", "presence_explicit_completion"), "add": {}}
EXPLICIT_ONLY_HOST = {"drop": ("presence_reaction_context",), "add": {}}
CAPABLE_HOST = {"drop": (), "add": {"presence_reaction_context": 1, "presence_explicit_completion": 1}}  # a stand-in, not proof


def _run(tmp_path, mode):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; the core Presence routes were not exercised")
    if not (Path(core) / "ouroboros" / "place_intake.py").read_text(encoding="utf-8").count("reaction_refusal"):
        pytest.skip("this core has no reaction fact; the routes were not exercised")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": os.pathsep.join([str(Path(core).resolve()), str(ROOT / "skills" / "slack-bridge"),
                                       str(ROOT / "skills" / "telegram-bot")]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run([sys.executable, __file__, str(tmp_path), mode], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_slack_reactions_through_the_real_routes(tmp_path):
    facts = _run(tmp_path, "slack")
    assert facts["refusals"] == {}
    assert facts["statuses"] == {"Ev-m": 200, "Ev-reply": 200, "Ev-r": 200, "Ev-on-reply": 200, "Ev-miss": 200, "Ev-removed": 200}
    # Only the ordinary message woke anybody, under a wake list that matches everything.
    assert facts["woken"] == ["Ev-m", "Ev-reply"]
    assert facts["reasons"] == {"Ev-r": "reaction_context_only", "Ev-on-reply": "reaction_context_only",
                                "Ev-miss": "reaction_context_only", "Ev-removed": "reaction_context_only"}
    assert facts["rows"]["Ev-on-reply"]["thread"] == "1.0"
    rows = facts["rows"]
    assert rows["Ev-r"]["text"] == "" and rows["Ev-r"]["thread"] == "1.0"
    assert "Reaction, not speech" in rows["Ev-r"]["read"] and "eyes" in rows["Ev-r"]["read"]
    assert rows["Ev-miss"]["thread"] == "" and "Reaction, not speech" in rows["Ev-miss"]["read"]
    assert rows["Ev-removed"]["read"].count("wave") == 1
    assert facts["candidates"] == ["U1", "U2"]  # individuals only; Slack has no chat or aggregate actor
    assert facts["inbox"]["Ev-r"] == "delivered" and facts["inbox"]["Ev-miss"] == "delivered"
    assert facts["receipts"] == {"react-ok": "delivered", "react-noop": "delivered", "react-gone": "failed"}
    assert facts["recorded"] == 3
    assert any("added reaction" in said for said in facts["actions"])
    assert any("no change" in said for said in facts["actions"])
    assert any("failed" in said for said in facts["actions"])


def test_telegram_reactions_through_the_real_routes(tmp_path):
    facts = _run(tmp_path, "telegram")
    assert facts["woken"] == ["telegram:9:1"]
    assert facts["reasons"] == {"telegram:9:2": "reaction_context_only", "telegram:9:3": "reaction_context_only",
                                "telegram:9:4": "reaction_context_only", "telegram:9:5": "reaction_context_only"}
    rows = facts["rows"]
    assert "Reaction, not speech" in rows["telegram:9:2"]["read"]
    assert rows["telegram:9:2"]["thread"] == "5"  # the retained message's topic
    assert rows["telegram:9:3"]["thread"] == ""  # unknown forum target: not General's author
    assert "chat" == rows["telegram:9:4"]["author_kind"] and "anonymous" == rows["telegram:9:5"]["author_kind"]
    assert facts["candidates"] == ["55"]  # never the chat or the aggregate
    assert facts["receipts"] == {"set": "delivered", "refused": "failed"}
    assert facts["recorded"] == 2
    assert any("set reaction" in said for said in facts["actions"])
    assert any("failed" in said for said in facts["actions"])


def test_old_core_receives_no_reaction_from_either_connector(tmp_path):
    facts = _run(tmp_path, "old-host")
    assert {"presence_reaction_context", "presence_explicit_completion"}.isdisjoint(facts["identity"])
    assert facts["slack_turns"] == ["Ev-m", "Ev-reply"] and facts["telegram_turns"] == ["telegram:9:1"]
    assert facts["slack_withheld"] == 4 and facts["telegram_withheld"] == 4
    assert facts["reports"] == 0 and facts["recorded"] == 0 and facts["actions"] == []
    assert facts["slack_applied"] == "applied" and facts["telegram_applied"] == "applied"


def test_explicit_completion_only_core_records_own_reactions_and_still_receives_none(tmp_path):
    facts = _run(tmp_path, "explicit-host")
    if facts["core_identity"].get("presence_explicit_completion") != 1:
        pytest.skip("this core does not advertise explicit completion; that Host was not exercised")
    assert facts["identity"].get("presence_explicit_completion") == 1 and "presence_reaction_context" not in facts["identity"]
    # Nothing incoming: without the reaction context each stays in custody, as on an old Host.
    assert facts["slack_turns"] == ["Ev-m", "Ev-reply"] and facts["telegram_turns"] == ["telegram:9:1"]
    assert facts["slack_withheld"] == 4 and facts["telegram_withheld"] == 4
    # Outgoing: three Slack and two Telegram receipts, each posted once and recorded as an action.
    assert facts["reports"] == 5 and facts["recorded"] == 5
    assert facts["actions"] == ["added reaction eyes, requested on message 1.0",
                                "no change: reaction eyes was already present, requested on message 1.0",
                                "failed: added reaction eyes, requested on message 1.0",
                                "set reaction 👍, requested on message 7", "failed: set reaction 👍, requested on message 7"]
    assert facts["slack_applied"] == "applied" and facts["telegram_applied"] == "applied"


def test_declared_reads_pass_the_real_guard_where_writes_do_not(tmp_path):
    facts = _run(tmp_path, "reads")
    for situation in ("advises", "silent", "paused"):
        row = facts[situation]
        assert row["slack_reactions"] == "" and row["slack_thread"] == ""
        assert row["slack_reaction_add"].startswith("⚠️ RESOURCE_CONSTRAINT_BLOCKED")
        # The generic method stays a write in the manifest, even when its caller selects a read.
        assert row["slack_api(effect=write)"].startswith("⚠️ RESOURCE_CONSTRAINT_BLOCKED")
        assert row["slack_api(effect=read)"].startswith("⚠️ RESOURCE_CONSTRAINT_BLOCKED")
    assert facts["requests"] == ["/api/reactions.get", "/api/conversations.replies"]
    assert facts["reactions"]["reactions"] == [{"name": "eyes", "count": 5, "users": ["U1"]}]
    reply = next(row for row in facts["thread"]["messages"] if row["ts"] == "1.5")
    assert reply["thread_ts"] == "1.0" and reply["user"] == "U2"


def test_frozen_list_report_is_not_posted_to_a_core_that_does_not_advertise_it(tmp_path):
    facts = _run(tmp_path, "frozen-held")
    assert "presence_explicit_completion" not in facts["identity"]
    assert facts["posts"] == 0 and facts["recorded"] == 0 and facts["acked"] is False
    assert facts["error"].startswith("Held:") and facts["provider_calls"] == 0 and facts["stored_shape"] == "list"


def test_frozen_list_report_is_recorded_by_a_core_advertising_only_explicit_completion(tmp_path):
    facts = _run(tmp_path, "frozen-explicit")
    if facts["core_identity"].get("presence_explicit_completion") != 1:
        pytest.skip("this core does not advertise explicit completion; that Host was not exercised")
    assert facts["identity"].get("presence_explicit_completion") == 1 and "presence_reaction_context" not in facts["identity"]
    assert facts["status"] == 200 and facts["posts"] == 1 and facts["recorded"] == 1 and facts["acked"] is True
    assert facts["stored_shape"] == "list" and facts["provider_calls"] == 0
    assert len(facts["actions"]) == 1 and "set reaction 👍" in facts["actions"][0]


def test_frozen_list_report_is_recorded_typed_by_a_real_core_and_reads_as_an_action(tmp_path):
    facts = _run(tmp_path, "frozen")
    assert facts["status"] == 200 and facts["recorded"] == 1 and facts["acked"] is True
    assert facts["stored_shape"] == "list" and facts["provider_calls"] == 0
    assert len(facts["actions"]) == 1 and "set reaction 👍" in facts["actions"][0]


def test_telegram_known_outcomes_reach_a_real_core_once_with_one_physical_call_each(tmp_path):
    facts = _run(tmp_path, "telegram-known")
    assert facts["provider_calls"] == {"7": 1, "8": 2}  # the album member is asked once per request
    assert facts["receipts"] == {"late-record": "delivered", "timeout": "uncertain", "album": "delivered"}
    assert facts["statuses"] == [200, 200, 200] and facts["recorded"] == 3
    assert facts["album_receipt"] == {"media_group_id": "album-1", "effective_message_id": "unknown"}
    assert facts["actions"] == ["set reaction 👍, requested on message 7",
                                "outcome unknown, may have landed, do not repeat: set reaction 👍, requested on message 8",
                                "set reaction 👍, requested on message 8"]
    assert facts["unacked"] == 0


def test_slack_padded_row_reaches_the_connected_place_with_one_address(tmp_path):
    facts = _run(tmp_path, "slack-padded")
    assert facts["writes"] == [{"channel": "C1", "timestamp": "1.5", "name": "eyes"}]
    assert facts["state"] == "delivered" and facts["recorded"] == 1
    assert facts["conversation"] == "C1" and facts["thread"] == "1.0"
    assert facts["target"] == {"message_id": "1.5", "resolution": "resolved"}
    assert any("added reaction" in said for said in facts["actions"])


def _refused(answer, code):
    return answer.get("ok") is False and code in answer["error"]


def _assert_unconfirmed(facts):
    answers = facts["answers"]
    assert facts["run_task"] is False and facts["requests"] == []
    # Retained or not, nothing can be read as this bot's, and the refusal says why rather than "not retained here".
    for label in ("plain", "plain_unretained", "forum_missing", "unobserved"):
        assert _refused(answers[label], "forum_topic_unknown"), (label, answers[label])
        assert "identity is unconfirmed" in answers[label]["error"]
        assert "not retained here" not in answers[label]["error"] and "not observed yet" not in answers[label]["error"]
    # What the caller supplies, and a private chat, stay queued with nothing claimed as proven.
    for label in ("forum_right", "forum_wrong", "private_plain", "private_topic_missing", "private_topic_right"):
        assert answers[label]["state"] == "queued", (label, answers[label])
        assert facts["targets"][label] == "unresolved", label


def test_pooled_worker_answers_from_the_bot_the_server_process_confirmed(tmp_path):
    server = _run(tmp_path, "server:token-a")
    assert server["run_task"] is True and server["confirmed"] == ["9", ""] and server["offset"] == 7
    facts = _run(tmp_path, "worker:token-a")
    # The process split itself: core loaded it here and never started its supervised task.
    assert facts["server_process"] is False and facts["run_task"] is False and facts["live"] is True
    assert facts["confirmed"] == ["", "Telegram bot not running"]
    assert facts["status"]["connect"] is None and facts["status"]["connect_unavailable"] == "Telegram bot not running"
    answers, guards = facts["answers"], facts["guards"]
    assert set(guards.values()) == {""}  # the place guard admitted every call on its active place
    assert set(facts["dispatched"].values()) == {True}  # and core's dispatch reached the handler each time
    for label in ("plain", "plain_unretained", "forum_right", "forum_general", "private_plain", "private_topic_right"):
        assert answers[label]["state"] == "queued", (label, answers[label])
        assert facts["targets"][label] == "resolved", label
    assert facts["queued_topics"]["forum_right"] == "5" and facts["queued_topics"]["plain"] is None
    for label in ("forum_wrong", "forum_missing", "private_topic_missing", "plain_wrong"):
        assert _refused(answers[label], "forum_topic_mismatch"), (label, answers[label])
    assert "topic_id=5" in answers["forum_missing"]["error"] and "topic_id=9" in answers["private_topic_missing"]["error"]
    assert _refused(answers["forum_unretained"], "forum_topic_unknown") and "is a forum" in answers["forum_unretained"]["error"]
    assert _refused(answers["unobserved"], "forum_topic_unknown") and "not observed yet" in answers["unobserved"]["error"]
    assert facts["requests"] == []  # no provider, Host or other request from the worker


def test_pooled_worker_does_not_trust_a_changed_never_confirmed_or_refused_token(tmp_path):
    _run(tmp_path, "server:token-a")
    # The settings moved on and no server process has asked Telegram about this token.
    _assert_unconfirmed(_run(tmp_path, "worker:token-new"))
    # The server asked and Telegram refused it; the bot of the earlier token still does not answer for it.
    assert _run(tmp_path, "server:token-refused")["confirmed"][0] == ""
    _assert_unconfirmed(_run(tmp_path, "worker:token-refused"))
    # The token Telegram confirmed before, until Telegram refuses that one too (revoked).
    confirmed_again = _run(tmp_path, "worker:token-a")
    assert confirmed_again["targets"]["plain"] == "resolved"
    assert _refused(confirmed_again["answers"]["forum_wrong"], "forum_topic_mismatch")
    assert _run(tmp_path, "server:token-a:revoked")["confirmed"][0] == ""
    _assert_unconfirmed(_run(tmp_path, "worker:token-a"))
    # A different bot's token is confirmed as that bot, whose records do not include the first bot's messages.
    assert _run(tmp_path, "server:token-b")["confirmed"] == ["77", ""]
    other = _run(tmp_path, "worker:token-b")
    assert _refused(other["answers"]["plain"], "forum_topic_unknown") and "not observed yet" in other["answers"]["plain"]["error"]
    assert "identity is unconfirmed" not in other["answers"]["plain"]["error"]


_BLOCKED = "⚠️ RESOURCE_CONSTRAINT_BLOCKED"


def test_telegram_reaction_queued_while_active_is_held_once_its_migrated_topic_is_paused(tmp_path):
    facts = _run(tmp_path, "paused-thread-telegram")
    # The owner's topic scope as core's legacy import keeps it, beneath an Active place for the whole chat.
    assert facts["places"] == {"room": ["-100", "", "active"], "thread": ["-100", "5", "paused"]}
    # At call time the guard reads the declared topic_id: Active admits it, Paused refuses it, General stays the room's.
    assert facts["guard"]["topic_active"] == "" and facts["guard"]["general_paused"] == ""
    assert facts["guard"]["topic_paused"].startswith(_BLOCKED) and "Paused" in facts["guard"]["topic_paused"]
    # Sent while Active, asking the Host about that exact topic.
    assert facts["active"]["receipts"] == {"active-thread": "delivered"} and facts["active"]["calls"] == {"7": 1}
    assert facts["active"]["queries"] == [{"provider": "telegram", "account_id": "9", "conversation_id": "-100", "thread_id": "5"}]
    # Queued while Active, the topic Paused before the worker: held, and Telegram is not asked for it.
    paused = facts["paused"]
    assert paused["receipts"] == {"active-thread": "delivered", "queued-while-active": "held", "room-only": "delivered"}
    assert paused["calls"] == {"7": 1, "8": 1}
    assert paused["held_error"].startswith("Held:") and "paused" in paused["held_error"]
    # A reaction queued without a topic asks about the chat alone, exactly as before.
    assert sorted(paused["queries"], key=len) == [
        {"provider": "telegram", "account_id": "9", "conversation_id": "-100"},
        {"provider": "telegram", "account_id": "9", "conversation_id": "-100", "thread_id": "5"}]
    # Active again: the held row is sent once, as every hold resumes.
    assert facts["resumed"]["receipts"]["queued-while-active"] == "delivered" and facts["resumed"]["calls"] == {"7": 2, "8": 1}


def test_slack_reaction_queued_while_active_is_held_once_its_migrated_thread_is_paused(tmp_path):
    facts = _run(tmp_path, "paused-thread-slack")
    assert facts["places"] == {"room": ["C1", "", "active"], "thread": ["C1", "1.0", "paused"]}
    # The tool declares only the channel, so the call-time guard admits a Paused thread's reply; the worker's re-read holds it.
    assert facts["guard"] == {"reply_active": "", "reply_paused": "", "top_paused": ""}
    assert facts["active"]["receipts"] == {"active-thread": "delivered"} and facts["active"]["writes"] == ["add 1.5"]
    assert facts["active"]["queries"] == [{"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "1.0"}]
    # Queued while Active, the thread Paused before the worker: held, and Slack is not asked for it.
    paused = facts["paused"]
    assert paused["receipts"] == {"active-thread": "delivered", "queued-while-active": "held"}
    assert paused["writes"] == ["add 1.5"] and paused["held_error"].startswith("Held:") and "paused" in paused["held_error"]
    assert paused["queries"] == [{"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "1.0"}]
    # The held reaction holds back only its own message: a reaction to another message and a top-level send in that
    # Active channel go now, with the send's receipt recorded by the Host; the same message's remove waits, never tried.
    behind = facts["behind_hold"]
    assert behind["receipts"] == {"queued-while-active": "held", "room-behind-hold": "delivered",
                                  "top-level-send": "delivered", "same-message-remove": "pending"}
    assert behind["writes"] == ["add 1.5", "add 3.0", "post independent"]
    assert behind["remove_attempts"] == 0 and behind["send_report"] == "acked"
    # Active again: the held add, then the remove queued behind it, once each.
    assert facts["resumed"]["receipts"] == {"queued-while-active": "delivered", "room-behind-hold": "delivered",
                                            "same-message-remove": "delivered"}
    assert facts["resumed"]["writes"] == ["add 1.5", "add 3.0", "post independent", "add 1.5", "remove 1.5"]
    # Paused again with nothing held ahead: a retained top-level target and an unretained one are the channel's and go;
    # a reply queued while Paused passes the call-time guard and is held here.
    again = facts["paused_again"]
    assert again["receipts"] == {"room-retained": "delivered", "room-unretained": "delivered", "queued-while-paused": "held"}
    assert again["writes"] == ["add 1.5", "add 3.0", "post independent", "add 1.5", "remove 1.5", "add 3.0", "add 9.0"]
    assert again["queries"] == [
        {"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "3.0"},
        {"provider": "slack", "account_id": "T1", "conversation_id": "C1"},
        {"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "1.0"}]


def test_slack_rows_from_before_the_update_keep_each_message_in_order_and_hold_back_nothing_else(tmp_path):
    facts = _run(tmp_path, "slack-lanes-upgrade")
    # The previous version laned each reaction by its channel; opening the store moves live ones to their message.
    assert facts["lanes_before"] == {"earlier-held": "C1:", "earlier-in-flight": "C1:", "earlier-crashed": "C1:",
                                     "earlier-same-message": " C1 :", "earlier-api-in-flight": "C1:", "earlier-text": "C1:"}
    assert facts["lanes_opened"] == {"earlier-held": "reaction:C1:1.5", "earlier-in-flight": "reaction:C1:3.0",
                                     "earlier-crashed": "reaction:C1:9.0", "earlier-same-message": "reaction:C1:1.5",
                                     "earlier-api-in-flight": "reaction:C1:5.0", "earlier-text": "C1:"}
    paused = facts["paused"]
    assert paused["receipts"] == {
        "earlier-held": "held", "earlier-in-flight": "leased", "earlier-crashed": "uncertain",
        "earlier-same-message": "pending", "earlier-api-in-flight": "leased", "earlier-text": "delivered",
        "after-upgrade": "delivered", "same-as-in-flight": "pending", "target-unknown": "held", "target-known": "pending",
        "api-same-as-in-flight": "pending", "api-get-independent": "delivered", "older-writer": "pending",
        "older-api-writer": "pending"}
    # Both top-level sends and a slack_api GET add to another message go at once; nothing that may have reached Slack
    # is sent again.
    assert paused["writes"] == ["post earlier words", "post after upgrade", "add 9.5"]
    assert paused["reports"]["earlier-text"] == paused["reports"]["after-upgrade"] == "acked"
    assert paused["errors"]["earlier-crashed"] == "send_result_unrecorded"
    assert paused["errors"]["earlier-held"].startswith("Held:") and paused["errors"]["target-unknown"].startswith("Held:")
    # Same-message writes, dedicated or slack_api, are never tried while one ahead is held or in an older worker's hands.
    waiting = ("earlier-same-message", "same-as-in-flight", "target-known", "api-same-as-in-flight", "older-writer",
               "older-api-writer")
    assert {key: paused["attempts"][key] for key in waiting} == dict.fromkeys(waiting, 0)
    # Rows an older process queued after the store opened, dedicated and slack_api, join their message's lane at a claim.
    assert paused["lanes"]["older-writer"] == paused["lanes"]["older-api-writer"] == "reaction:C1:1.5"
    done = facts["in_flight_done"]
    assert {key: done["receipts"][key] for key in ("earlier-in-flight", "same-as-in-flight", "earlier-api-in-flight",
                                                   "api-same-as-in-flight")} == dict.fromkeys(
        ("earlier-in-flight", "same-as-in-flight", "earlier-api-in-flight", "api-same-as-in-flight"), "delivered")
    assert done["writes"] == ["post earlier words", "post after upgrade", "add 9.5", "remove 3.0", "remove 5.0"]
    assert facts["restarted"]["receipts"] == done["receipts"] and facts["restarted"]["writes"] == done["writes"]
    resumed = facts["resumed"]
    assert resumed["receipts"] == {**done["receipts"], "earlier-held": "delivered", "earlier-same-message": "delivered",
                                   "target-unknown": "delivered", "target-known": "delivered", "older-writer": "delivered",
                                   "older-api-writer": "delivered"}
    # Each message in its queue order: 1.5's add, then its three removes (the last the slack_api GET);
    # 7.0's add, queued before its record, then its remove.
    assert resumed["writes"][5:] == ["add 1.5", "remove 1.5", "add 7.0", "remove 7.0", "remove 1.5", "remove 1.5"]
    assert "add 9.0" not in resumed["writes"]


# ---- subprocess -----------------------------------------------------------------------------

def _core(root, connector, provider, account, conversation, *, host=CAPABLE_HOST):
    from types import SimpleNamespace

    import httpx

    from ouroboros import places as p, presence_admission, presence_context
    from ouroboros.gateway import host_service

    data = root / "data"
    p.connect_place(data, connector=connector, provider=provider, account_id=account, conversation_id=conversation,
                    wakes=["any_message", "from_bot"])
    presence_admission.focus_enabled = lambda _root: True
    presence_admission.focus_allowance = lambda _root: {"status": "available", "remaining_usd": 2, "settled_usd": 1}
    presence_context._own_work_section = lambda *_args: ""

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token == TOKEN
        return connector, {}
    host_service._authenticated = authenticated
    woken = []

    def runner(**kwargs):
        woken.append(kwargs["event"].source_event_id)
        return SimpleNamespace(outcome="silent", text="", task_id="focus", work_ref="")

    app = host_service.create_host_service_app(data, presence_runner=runner)
    inner = httpx.ASGITransport(app=app)
    world = SimpleNamespace(data=data, woken=woken, statuses={}, refusals={}, turns=[], reports=0, recorded=0,
                            core_identity={}, identity={})

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            path = request.url.path
            response = await inner.handle_async_request(request)
            if path in {"/identity", "/presence/turn", "/presence/delivery"}:
                body = await response.aread()
                if path == "/identity":
                    # The Host described in this module's docstring, over core's own answer.
                    world.core_identity = json.loads(body)
                    world.identity = {**{key: value for key, value in world.core_identity.items() if key not in host["drop"]},
                                      **host["add"]}
                    return httpx.Response(response.status_code, json=world.identity)
                if path == "/presence/turn":
                    source = json.loads(request.content)["event"]["source_event_id"]
                    world.turns.append(source)
                    world.statuses[source] = response.status_code
                    if response.status_code >= 400:
                        world.refusals[source] = json.loads(body)
                if path == "/presence/delivery":
                    world.reports += 1
                    world.recorded += int(json.loads(body).get("recorded") is True)
                return httpx.Response(response.status_code, content=body, headers=response.headers)
            return response
    world.transport = Transport()
    return world


async def _settle_slack(worker, store):
    """Reports run beside sending and a real route takes real time, so wait for them, within bounds."""
    import asyncio
    import sqlite3

    for _ in range(400):
        await worker.process_once()
        await asyncio.sleep(0.01)
        with sqlite3.connect(store.path) as db:
            busy = db.execute("SELECT COUNT(*) FROM outbox WHERE state IN ('pending','leased') "
                              "OR report_state IN ('pending','reporting')").fetchone()[0]
        if not busy:
            return


async def _reported(worker, store):
    """Let history reports already due reach the real route, within bounds; a held row is not waited for."""
    import asyncio
    import sqlite3

    for _ in range(400):
        with sqlite3.connect(store.path) as db:
            if not db.execute("SELECT COUNT(*) FROM outbox WHERE report_state IN ('pending','reporting')").fetchone()[0]:
                return
        await worker.process_once()
        await asyncio.sleep(0.01)


def _readback(world):
    from ouroboros import dialogue_store
    from ouroboros.dialogue_provenance import reaction_action_words, row_author, dialogue_text, reaction_fact
    from ouroboros.place_arrivals import account_candidates

    rows, reasons, actions = {}, {}, []
    with dialogue_store.transaction(world.data, write=False) as conn:
        dispatch = {str(row["client_message_id"]): json.loads(row["dispatch_input"] or "{}").get("place_attention")
                    for row in conn.execute("SELECT client_message_id,dispatch_input FROM dialogue WHERE stream='chat' AND direction='in'")}
    for entry in dialogue_store.iter_rows(world.data, "chat"):
        row = entry.row
        transport = row.get("transport") or {}
        if row.get("direction") == "in" and reaction_fact(transport.get("message")):
            source = str(row.get("client_message_id") or "")
            rows[source] = {"text": row.get("text"), "read": dialogue_text(row), "thread": str(transport.get("thread_id") or ""),
                            "author_kind": row_author(row).get("actor_kind", row_author(row).get("kind"))}
            reasons[source] = (dispatch.get(source) or {}).get("reason")
        if row.get("type") == "presence_delivery" and reaction_fact(transport.get("message")):
            actions.append(reaction_action_words(reaction_fact(transport.get("message")), str((transport.get("delivery") or {}).get("state") or "")))
    return rows, reasons, actions, account_candidates


def _slack(root, *, host=CAPABLE_HOST):
    import asyncio
    import sqlite3

    import httpx

    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import InboundWorker, OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore

    world = _core(root, "slack-bridge", "slack", "T1", "", host=host)
    store = BridgeStore(root / "slack-state")
    answers = [{"ok": True}, {"ok": False, "error": "already_reacted"}, {"ok": False, "error": "message_not_found"}]

    def provider(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"id": request.url.params["user"], "name": "n"}})
        if path == "conversations.replies":
            if "oldest" in request.url.params:
                return httpx.Response(200, json={"ok": False, "error": "thread_not_found"})
            return httpx.Response(200, json={"ok": True, "messages": [{"ts": "1.0", "user": "U1", "text": "hi"}], "has_more": False})
        if path.startswith("reactions."):
            return httpx.Response(200, json=answers.pop(0))
        return httpx.Response(200, json={"ok": True, "channel": {"id": request.url.params["channel"]}})

    def reaction(event_id, ts, *, kind="reaction_added", name="eyes", user="U2"):
        return {"type": "events_api", "envelope_id": event_id, "payload": {"event_id": event_id, "team_id": "T1", "event": {
            "type": kind, "user": user, "reaction": name, "event_ts": "2.0", "item_user": "U1",
            "item": {"type": "message", "channel": "C1", "ts": ts}}}}
    message = {"type": "events_api", "envelope_id": "Ev-m", "payload": {"event_id": "Ev-m", "team_id": "T1", "event": {
        "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": "1.0", "text": "hi"}}}
    reply = {"type": "events_api", "envelope_id": "Ev-reply", "payload": {"event_id": "Ev-reply", "team_id": "T1", "event": {
        "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": "1.5", "thread_ts": "1.0", "text": "more"}}}
    for payload in (message, reply, reaction("Ev-r", "1.0"), reaction("Ev-on-reply", "1.5"), reaction("Ev-miss", "9.0"),
                    reaction("Ev-removed", "1.0", kind="reaction_removed", name="wave")):
        store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=world.transport, timeout=60.0) as host_http:
            slack = SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http)
            adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                                  skill_token=TOKEN, http_client=host_http)
            inbound = InboundWorker(store, slack, adapter, staged_root=root / "staged")
            for _ in range(8):
                await inbound.process_once()
            await _discover_reporting(adapter, store)
            store.set_runtime(workspace_id="T1")
            for request_id, operation in (("react-ok", "reaction_add"), ("react-noop", "reaction_add"), ("react-gone", "reaction_add")):
                store.enqueue_mutation(request_id=request_id, operation=operation,
                                       payload={"channel": "C1", "ts": "1.0", "name": "eyes"},
                                       origin={"kind": "tool"}, delivery_reporting_version=1)
            outbound = OutboundWorker(store, slack, adapter)
            await _settle_slack(outbound, store)
            await outbound.aclose()
    asyncio.run(work())
    with sqlite3.connect(store.path) as db:
        inbox = {row[0]: row[1] for row in db.execute("SELECT event_id,state FROM inbox")}
    return world, store, inbox


def _telegram(root, *, host=CAPABLE_HOST):
    import asyncio
    import json as _json

    import httpx

    from telegram_bot.api import TelegramClient
    from telegram_bot.events import parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime
    from telegram_bot.tools import make_operation_tool
    from types import SimpleNamespace

    world = _core(root, "telegram-bot", "telegram", "9", "", host=host)
    answers = [{"ok": True, "result": True}, {"ok": False, "error_code": 400, "description": "Bad Request: REACTION_INVALID"}]

    class Provider:
        calls = []

        async def post_json(self, url, payload, timeout_sec):
            self.calls.append(url.rsplit("/", 1)[-1])
            return answers.pop(0) if answers else {"ok": True, "result": True}

    class HostTransport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
            body = response.json()
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = root / "telegram-state"
    client = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                                transport=HostTransport())
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=client,
                                       client_factory=lambda token: TelegramClient(token, transport=Provider()))
    runtime._client, runtime._client_token, runtime._bot_id = TelegramClient("fake", transport=Provider()), "fake", "9"
    forum = {"id": -100, "type": "supergroup", "title": "Forum", "is_forum": True}
    ann = {"id": 55, "first_name": "Ann"}
    updates = [
        {"update_id": 1, "message": {"message_id": 7, "date": 1, "chat": forum, "from": ann, "text": "hello",
                                     "message_thread_id": 5, "is_topic_message": True}},
        {"update_id": 2, "message_reaction": {"chat": forum, "message_id": 7, "user": ann, "date": 2,
                                              "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
        {"update_id": 3, "message_reaction": {"chat": forum, "message_id": 999, "user": ann, "date": 3,
                                              "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
        {"update_id": 4, "message_reaction": {"chat": forum, "message_id": 7, "actor_chat": {"id": -77, "title": "News", "type": "channel"},
                                              "date": 4, "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "❤"}]}},
        {"update_id": 5, "message_reaction_count": {"chat": forum, "message_id": 7, "date": 5,
                                                    "reactions": [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 3}]}},
    ]

    async def work():
        await client.discover_delivery_support()
        for update in updates:
            runtime.store.commit_update(update["update_id"], parse_telegram_update(update, bot_account_id="9", places=True))
        for _ in range(8):
            await runtime.process_one_inbox()
        operate = make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(state)), lambda: "9")
        for request_id in ("set", "refused"):
            result = operate(method="setMessageReaction", chat_id="-100", message_id=7, topic_id=5,
                             reaction=[{"type": "emoji", "emoji": "👍"}], request_id=request_id)
            assert result["state"] == "queued", result
        for _ in range(4):
            await runtime.process_one_outbox()
        while await runtime.process_one_delivery_report():
            pass
    asyncio.run(work())
    return world, runtime


def _reads(root):
    import asyncio
    from types import SimpleNamespace

    import httpx
    import yaml

    from lib import read_tools
    from lib.slack_api import SlackClient
    from ouroboros import extension_registry_state as registry, place_guard, places as p
    from ouroboros.extension_surface_names import extension_surface_name

    manifest = yaml.safe_load((ROOT / "skills/slack-bridge/SKILL.md").read_text(encoding="utf-8").split("---")[1])
    bundle = SimpleNamespace(generation_digest="g", loaded_skill=SimpleNamespace(
        manifest=SimpleNamespace(raw_extra={"tools": manifest["tools"]})))
    registry._extensions["slack-bridge"] = bundle
    tool = {"skill": "slack-bridge", "extension_generation": "g"}
    data = root / "data"
    place = p.connect_place(data, connector="slack-bridge", provider="slack", account_id="T1", conversation_id="C1",
                            wakes=["any_message"])
    reads = {"channel_id": "C1", "ts": "1.5"}
    calls = {"slack_reactions": ("slack_reactions", reads),
             "slack_thread": ("slack_thread", {"channel_id": "C1", "thread_ts": "1.5"}),
             "slack_reaction_add": ("slack_reaction_add", {"channel": "C1", "ts": "1.5", "name": "eyes"}),
             "slack_api(effect=write)": ("slack_api", {"path": "reactions.add", "effect": "write"}),
             "slack_api(effect=read)": ("slack_api", {"path": "reactions.get", "effect": "read"})}

    def refusals(metadata):
        ctx = SimpleNamespace(drive_root=data, task_id="caller", task_metadata=metadata)
        return {label: place_guard.dispatch_refusal(ctx, extension_surface_name("slack-bridge", short), args, ext_tool=tool)
                for label, (short, args) in calls.items()}

    facts = {"advises": refusals({"initiator": "consciousness", "consciousness_autonomy": "observe"})}
    for state in ("silent", "paused"):  # an ordinary task, which Advises does not narrow
        p.edit_place(data, place.place_id, "set_state", author="owner", state=state)
        facts[state] = refusals({})
    requests = []

    def provider(request):
        requests.append(request.url.path)
        if request.url.path.endswith("reactions.get"):
            return httpx.Response(200, json={"ok": True, "type": "message", "channel": "C1", "message": {
                "ts": "1.5", "text": "r", "reactions": [{"name": "eyes", "count": 5, "users": ["U1"]}]}})
        return httpx.Response(200, json={"ok": True, "has_more": False, "messages": [
            {"ts": "1.0", "thread_ts": "1.0", "user": "U9", "text": "root"},
            {"ts": "1.5", "thread_ts": "1.0", "user": "U2", "text": "r"}]})

    class Api:
        tools = {}

        def get_settings(self, keys):
            return {"SLACK_BOT_TOKEN": "xoxb-fixture", "SLACK_APP_TOKEN": "xapp-fixture"}

        def register_tool(self, name, handler, **metadata):
            self.tools[name] = handler

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            read_tools.SlackClient = lambda bot, app: SlackClient(bot, app, http_client=http)
            api = Api()
            read_tools.register_read_tools(api)
            return [json.loads(await api.tools[name](**args)) for name, args in (
                ("slack_reactions", reads), ("slack_thread", {"channel_id": "C1", "thread_ts": "1.5"}))]
    facts["reactions"], facts["thread"] = asyncio.run(work())
    return {**facts, "requests": requests}


def _frozen(root, *, host=CAPABLE_HOST):
    import asyncio

    import httpx

    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime

    world = _core(root, "telegram-bot", "telegram", "9", "", host=host)

    class HostTransport:
        status = 0

        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
            body = response.json()
            if url.endswith("/presence/delivery"):
                HostTransport.status = response.status_code
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = root / "telegram-state"
    client = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                                transport=HostTransport())
    provider_calls = []

    class NoTelegram:
        async def operation(self, *args):
            provider_calls.append(args)
            raise AssertionError("a frozen report never reaches Telegram")
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=client,
                                       client_factory=lambda _token: NoTelegram())
    report = {"schema_version": 1, "delivery_id": "telegram-operation:old", "part_id": "0", "state": "delivered",
              "provider": "telegram", "account_id": "9", "conversation_id": "-200", "thread_id": "", "text": "", "format": "",
              "message": {"kind": "operation", "method": "setMessageReaction", "target_message_id": 7,
                          "reaction": [{"type": "emoji", "emoji": "👍"}]},
              "origin": {"kind": "tool"}}
    runtime.store.enqueue_outbox(report["delivery_id"], {
        "kind": "operation", "chat_id": "-200", "method": "setMessageReaction", "text": "",
        "parameters": {"chat_id": "-200", "message_id": 7, "reaction": report["message"]["reaction"]},
        "_reporting": {"version": 1, "account_id": "9", "origin": {"kind": "tool"}}, "_operation_result": {"ok": True}})
    runtime.store.claim_outbox(lease_sec=120)
    runtime.store.mark_delivered(report["delivery_id"], provider_receipt={"result": {"ok": True}}, report=report)

    async def work():
        while await runtime.process_one_delivery_report():
            pass
    asyncio.run(work())
    [entry] = runtime.store.delivery_receipt(report["delivery_id"])["history_reports"]
    _rows, _reasons, actions, _candidates = _readback(world)
    return {"status": HostTransport.status, "posts": world.reports, "recorded": world.recorded, "acked": entry["acked"],
            "error": entry["error"],
            "stored_shape": type(entry["payload"]["message"]["reaction"]).__name__, "provider_calls": len(provider_calls),
            "actions": actions, "core_identity": world.core_identity, "identity": world.identity}


def _telegram_known(root):
    """A confirmed set whose record fails, an ambiguous answer, and an album member, on the real routes."""
    import asyncio
    import sqlite3

    import httpx

    from telegram_bot.api import TelegramClient
    from telegram_bot.events import parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime
    from telegram_bot.tools import make_operation_tool
    from types import SimpleNamespace

    world = _core(root, "telegram-bot", "telegram", "9", "")
    answers = [{"ok": True, "result": True}, {"ok": False, "error_code": 408, "description": "Request Timeout"},
               {"ok": True, "result": True}]
    calls, statuses = [], []

    class Provider:
        async def post_json(self, url, payload, timeout_sec):
            calls.append(payload)
            return answers.pop(0)

    class HostTransport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
            if url.endswith("/presence/delivery"):
                statuses.append(response.status_code)
            body = response.json()
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = root / "telegram-state"
    host = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                              transport=HostTransport())
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=host)
    runtime._client, runtime._client_token, runtime._bot_id = TelegramClient("fake", transport=Provider()), "fake", "9"
    store = runtime.store
    group = {"id": -200, "type": "supergroup", "title": "Plain"}
    for update_id, message_id, album in ((1, 7, {}), (2, 8, {"media_group_id": "album-1"})):
        update = {"update_id": update_id, "message": {"message_id": message_id, "date": 1, "chat": group, "text": "words",
                                                      "from": {"id": 55, "first_name": "Ann"}, **album}}
        store.commit_update(update_id, parse_telegram_update(update, bot_account_id="9", places=True))
    operate = make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(state)), lambda: "9")
    for request_id, message_id in (("late-record", 7), ("timeout", 8), ("album", 8)):
        queued = operate(method="setMessageReaction", chat_id="-200", message_id=message_id,
                         reaction=[{"type": "emoji", "emoji": "👍"}], request_id=request_id)
        assert queued["state"] == "queued", queued
    real_checkpoint, failed = store.checkpoint_outbox, []

    def fails_once_on_the_result(delivery_id, payload, **kwargs):
        if "_operation_result" in payload and not failed:
            failed.append(delivery_id)
            raise sqlite3.OperationalError("database is locked")
        return real_checkpoint(delivery_id, payload, **kwargs)
    store.checkpoint_outbox = fails_once_on_the_result

    def everything_due():
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")

    async def work():
        await host.discover_delivery_support()
        for _ in range(5):
            await runtime.process_one_outbox()
            everything_due()
        while await runtime.process_one_delivery_report():
            pass
    asyncio.run(work())
    _rows, _reasons, actions, _candidates = _readback(world)
    keys = ("late-record", "timeout", "album")
    receipts = {key: store.delivery_receipt(f"telegram-operation:{key}") for key in keys}
    per_message = {}
    for call in calls:
        per_message[str(call["message_id"])] = per_message.get(str(call["message_id"]), 0) + 1
    return {"provider_calls": per_message, "receipts": {key: row["state"] for key, row in receipts.items()},
            "statuses": statuses, "recorded": world.recorded, "actions": actions,
            "album_receipt": receipts["album"]["provider_receipt"].get("album"),
            "unacked": sum(not entry["acked"] for row in receipts.values() for entry in row["history_reports"])}


def _slack_padded(root):
    """A row queued with padded arguments before this version, on a place connected to exactly C1."""
    import asyncio
    import sqlite3

    import httpx

    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore

    world = _core(root, "slack-bridge", "slack", "T1", "C1")
    store = BridgeStore(root / "slack-state")
    store.set_runtime(workspace_id="T1")
    reply = {"type": "events_api", "envelope_id": "Ev-reply", "payload": {"event_id": "Ev-reply", "team_id": "T1", "event": {
        "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": "1.5", "thread_ts": "1.0", "text": "more"}}}
    store.ingest_envelope(reply, parse_socket_envelope(reply, bot_user_id="U_BOT"))
    with sqlite3.connect(store.path) as db:
        db.execute(
            """INSERT INTO outbox(request_id,chunk_index,chunk_count,target,thread_ts,text,ordering_key,kind,operation,
                                  payload_json,origin_json,delivery_reporting_version,created_at,updated_at)
               VALUES('padded',0,1,' C1 ','','',' C1 :','mutation','reaction_add',?,?,1,1,1)""",
            (json.dumps({"channel": " C1 ", "ts": " 1.5 ", "name": " eyes "}), json.dumps({"kind": "tool"})))
    writes, posted = [], []

    def provider(request):
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    class Recording(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/presence/delivery":
                posted.append(json.loads(request.content))
            return await world.transport.handle_async_request(request)

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=Recording(), timeout=60.0) as host_http:
            adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                                  skill_token=TOKEN, http_client=host_http)
            worker = OutboundWorker(store, SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http), adapter)
            await _discover_reporting(adapter, store)
            await _settle_slack(worker, store)
            await worker.aclose()
    asyncio.run(work())
    _rows, _reasons, actions, _candidates = _readback(world)
    report = posted[0] if posted else {"message": {}}
    return {"writes": writes, "state": store.delivery_receipt("padded")["parts"][0]["state"], "recorded": world.recorded,
            "conversation": report.get("conversation_id"), "thread": report.get("thread_id"), "actions": actions,
            "target": (report["message"].get("reaction") or {}).get("target")}


# Telegram's answers behind the real client: getMe per token, then these messages for bot 9.
_BOTS = {"token-a": {"id": 9, "is_bot": True, "username": "presence_bot"},
         "token-b": {"id": 77, "is_bot": True, "username": "other_bot"}}
_GROUP = {"id": -200, "type": "supergroup", "title": "Plain"}
_FORUM = {"id": -100, "type": "supergroup", "title": "Forum", "is_forum": True}
_DM_TOPICS = {"id": 55, "type": "private", "first_name": "Ann"}
_DM_PLAIN = {"id": 56, "type": "private", "first_name": "Bob"}


def _message(update_id, chat, message_id, topic=None):
    return {"update_id": update_id, "message": {
        "message_id": message_id, "date": 1, "chat": chat, "from": {"id": 55, "is_bot": False, "first_name": "Ann"},
        "text": "words", **({"message_thread_id": topic, "is_topic_message": True} if topic else {})}}


_UPDATES = [_message(1, _GROUP, 7), _message(2, _FORUM, 7, topic=5), _message(3, _FORUM, 8), _message(4, _DM_TOPICS, 3, topic=9),
            _message(5, _DM_PLAIN, 4), {"update_id": 6, "my_chat_member": {
                "chat": _GROUP, "from": {"id": 55, "is_bot": False}, "date": 1,
                "old_chat_member": {"status": "left", "user": {"id": 9, "is_bot": True}},
                "new_chat_member": {"status": "member", "user": {"id": 9, "is_bot": True}}}}]
_WORKER_CALLS = {
    "plain": ("-200", 7, None), "plain_wrong": ("-200", 7, 3), "plain_unretained": ("-200", 999, None),
    "unobserved": ("-300", 2, None),
    "forum_right": ("-100", 7, 5), "forum_wrong": ("-100", 7, 6), "forum_missing": ("-100", 7, None),
    "forum_general": ("-100", 8, None), "forum_unretained": ("-100", 999, None),
    "private_topic_right": ("55", 3, 9), "private_topic_missing": ("55", 3, None), "private_plain": ("56", 4, None),
}


def _scripted_network(requests, *, bots, updates=()):
    """Everything the skill's urllib clients would send; ``requests`` lists what they asked for."""
    import io
    import time
    import urllib.error
    import urllib.request

    def urlopen(request, timeout=None):
        url = request.full_url
        requests.append(url)
        if "/bot" in url:
            token, method = url.split("/bot", 1)[1].rsplit("/", 1)
            bot = bots.get(token)
            if bot is None:
                body = json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(body))
            if method == "getMe":
                result = bot
            elif method == "getUpdates":
                offset = json.loads(request.data)["offset"]
                time.sleep(0.02)
                result = [update for update in updates if update["update_id"] >= offset]
            else:
                result = True
            return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())
        if url.endswith("/identity"):
            return io.BytesIO(json.dumps({"presence_delivery_version": 1, "places_version": 1}).encode())
        body = json.dumps({"ok": False, "error": "Connect this place in Consciousness > Places.",
                           "code": "place_not_connected", "field": "place", "disposition": "blocked"}).encode()
        raise urllib.error.HTTPError(url, 409, "Conflict", {}, io.BytesIO(body))
    urllib.request.urlopen = urlopen


def _load_skill(root, token):
    """This Hub's skill, reviewed, enabled and granted as a real install is, loaded by core's own reload_all."""
    import shutil

    from ouroboros import extension_loader
    from ouroboros.config import load_settings
    from ouroboros.skill_loader import SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants

    os.environ["OUROBOROS_RUNTIME_MODE"] = "advanced"
    os.environ["OUROBOROS_HOST_SERVICE_PORT"] = "1"  # never the live Host's port, whatever the fakes miss
    data, skills = root / "data", root / "skills"
    Path(os.environ["OUROBOROS_SETTINGS_PATH"]).write_text(json.dumps({"TELEGRAM_PUBLIC_BOT_TOKEN": token}), encoding="utf-8")
    if not skills.exists():
        shutil.copytree(ROOT / "skills" / "telegram-bot", skills / "telegram-bot",
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".DS_Store"))
        loaded = find_skill(data, "telegram-bot", repo_path=str(skills))
        save_enabled(data, "telegram-bot", True)
        save_review_state(data, "telegram-bot", SkillReviewState(status="pass", content_hash=loaded.content_hash))
        save_skill_grants(data, "telegram-bot", ["TELEGRAM_PUBLIC_BOT_TOKEN"], content_hash=loaded.content_hash,
                          requested_keys=["TELEGRAM_PUBLIC_BOT_TOKEN"], granted_permissions=["presence"],
                          requested_permissions=["presence"])
    errors = extension_loader.reload_all(data, load_settings, repo_path=str(skills))
    assert errors.get("telegram-bot") is None, errors
    return next(module for module in list(sys.modules.values()) if hasattr(module, "_make_telegram_send"))


def _server_phase(root, token, revoked):
    """The server process: core starts the skill's supervised task, which polls until the updates are committed."""
    import asyncio
    import threading
    import time

    from ouroboros import extension_loader
    from ouroboros.event_bus import get_global_event_bus
    from ouroboros.extension_companion import init_server_process_pid

    requests = []
    # A revoked token is the same string Telegram no longer answers for.
    bots = {} if revoked else _BOTS
    _scripted_network(requests, bots=bots, updates=_UPDATES if token == "token-a" else ())
    init_server_process_pid()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    get_global_event_bus().set_loop(loop)
    plugin = _load_skill(root, token)
    runtime = plugin._RUNTIME

    def settled():
        if token not in bots:
            return runtime.status_snapshot()["runtime_state"] == "provider_error"
        return runtime.confirmed_bot()[0] and (token != "token-a" or runtime.store.telegram_offset() > len(_UPDATES))

    deadline = time.monotonic() + 60
    while not settled():
        assert time.monotonic() < deadline, requests
        time.sleep(0.02)
    facts = {"run_task": runtime._run_task is not None, "confirmed": list(runtime.confirmed_bot()),
             "offset": runtime.store.telegram_offset()}
    extension_loader.unload_extension("telegram-bot")
    return facts


def _worker_phase(root, token):
    """A pooled worker: core's real reload_all, no supervised task, and each call through the real dispatch."""
    import ast
    import sqlite3
    from types import SimpleNamespace

    from ouroboros import extension_loader, place_guard, places as p, safety
    from ouroboros.extension_companion import is_server_process
    from ouroboros.extension_surface_names import extension_surface_name
    from ouroboros.tools.extension_dispatch import _dispatch_extension_tool_result

    requests = []
    _scripted_network(requests, bots={})
    safety.check_safety = lambda *_args, **_kwargs: (True, "")
    data = root / "data"
    plugin = _load_skill(root, token)
    runtime = plugin._RUNTIME
    connected = {place.conversation_id for place in p.load_places(data)}
    for chat in {call[0] for call in _WORKER_CALLS.values()} - connected:
        for account in ("9", "77"):
            place = p.connect_place(data, connector="telegram-bot", provider="telegram", account_id=account, conversation_id=chat,
                                    wakes=["any_message"])
            p.edit_place(data, place.place_id, "set_state", author="owner", state="active")
    name = extension_surface_name("telegram-bot", "telegram_operation")
    ext_tool = extension_loader.get_tool(name)
    ctx = SimpleNamespace(drive_root=data, task_id="pooled-task", task_metadata={}, messages=None)
    guards, answers, dispatched, targets, topics = {}, {}, {}, {}, {}
    for label, (chat, message, topic) in _WORKER_CALLS.items():
        request_id = f"{label}-{os.getpid()}"
        args = {"method": "setMessageReaction", "chat_id": chat, "message_id": message, "request_id": request_id,
                "reaction": [{"type": "emoji", "emoji": "👍"}], **({"topic_id": topic} if topic else {})}
        guards[label] = place_guard.dispatch_refusal(ctx, name, args, ext_tool=ext_tool)
        result = _dispatch_extension_tool_result(ctx, name, ext_tool, args)
        dispatched[label], answers[label] = result.meta.get("physical_dispatch"), ast.literal_eval(result.text)
        with sqlite3.connect(runtime.store.path) as db:
            row = db.execute("SELECT payload_json FROM outbox WHERE delivery_id=?", (f"telegram-operation:{request_id}",)).fetchone()
        if row:
            payload = json.loads(row[0])
            targets[label], topics[label] = payload["_reaction_target"]["resolution"], payload.get("topic_id")
    return {"server_process": is_server_process(), "run_task": runtime._run_task is not None,
            "live": extension_loader.is_extension_live("telegram-bot", data, repo_path=str(root / "skills")),
            "confirmed": list(runtime.confirmed_bot()), "status": plugin._status_payload(), "guards": guards,
            "answers": answers, "dispatched": dispatched, "targets": targets, "queued_topics": topics, "requests": requests}


def _migrated_thread(data, *, connector, provider, account, conversation, thread):
    """A thread-scoped legacy binding imported by core's real migration, its only source of thread places."""
    from dataclasses import asdict
    from types import SimpleNamespace

    from ouroboros import places as p, presence_bindings as b, skill_loader

    endpoint = b.PresenceEndpoint(provider, account, conversation, thread)
    binding = b.PresenceBinding(f"{1:032x}", connector, "legacy-profile", endpoint, endpoint, True)
    source = data / "state" / b.PRESENCE_BINDINGS_FILENAME
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({"schema_version": b.PRESENCE_BINDINGS_SCHEMA_VERSION,
                                  "bindings": {binding.binding_id: asdict(binding)}}), encoding="utf-8")
    # The old behavior skill, as core's own migration test stands it in; the transport adds no wakes here.
    profile = SimpleNamespace(skill_dir=data, manifest=SimpleNamespace(
        raw_extra={"presence": {"instructions": "Legacy words"}}, contributes=[]))
    real = skill_loader.find_skill
    skill_loader.find_skill = lambda _root, name, **_kwargs: profile if name == "legacy-profile" else None
    try:
        [place] = p.load_places(data)
    finally:
        skill_loader.find_skill = real
    assert place.thread_id == thread and place.legacy_binding_ids == (binding.binding_id,), place
    return place


def _guard(data, skill, short, args):
    """Core's real call-time place guard over this Hub's declared tool targets."""
    from types import SimpleNamespace

    import yaml

    from ouroboros import extension_registry_state as registry, place_guard
    from ouroboros.extension_surface_names import extension_surface_name

    manifest = yaml.safe_load((ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8").split("---")[1])
    registry._extensions[skill] = SimpleNamespace(generation_digest="g", loaded_skill=SimpleNamespace(
        manifest=SimpleNamespace(raw_extra={"tools": manifest["tools"]})))
    ctx = SimpleNamespace(drive_root=data, task_id="caller", task_metadata={})
    return place_guard.dispatch_refusal(ctx, extension_surface_name(skill, short), args,
                                        ext_tool={"skill": skill, "extension_generation": "g"})


def _places_by_scope(data):
    from ouroboros import places as p
    return {("thread" if place.thread_id else "room"): [place.conversation_id, place.thread_id, place.state]
            for place in p.load_places(data)}


def _paused_thread_telegram(root):
    """Queued while its topic is Active, the topic Paused before the worker; General and resume beside it."""
    import asyncio
    import sqlite3
    import urllib.parse
    from types import SimpleNamespace

    import httpx

    from ouroboros import places as p
    from telegram_bot.api import TelegramClient
    from telegram_bot.events import parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime
    from telegram_bot.tools import make_operation_tool

    data = root / "data"
    thread = _migrated_thread(data, connector="telegram-bot", provider="telegram", account="9", conversation="-100", thread="5")
    world = _core(root, "telegram-bot", "telegram", "9", "-100")
    calls, queries = [], []

    class Provider:
        async def post_json(self, url, payload, timeout_sec):
            calls.append(str(payload["message_id"]))
            return {"ok": True, "result": True}

    class HostTransport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            if "/presence/place?" in url:
                queries.append(dict(urllib.parse.parse_qsl(url.split("?", 1)[1])))
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
            body = response.json()
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = root / "telegram-state"
    host = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                              transport=HostTransport())
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=host)
    runtime._client, runtime._client_token, runtime._bot_id = TelegramClient("fake", transport=Provider()), "fake", "9"
    forum = {"id": -100, "type": "supergroup", "title": "Forum", "is_forum": True}
    for update_id, message_id, topic in ((1, 7, 5), (2, 8, None)):
        update = {"update_id": update_id, "message": {
            "message_id": message_id, "date": 1, "chat": forum, "from": {"id": 55, "first_name": "Ann"}, "text": "words",
            **({"message_thread_id": topic, "is_topic_message": True} if topic else {})}}
        runtime.store.commit_update(update_id, parse_telegram_update(update, bot_account_id="9", places=True))
    operate = make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(state)), lambda: "9")
    thumb = [{"type": "emoji", "emoji": "👍"}]
    in_topic, in_general = {"chat_id": "-100", "message_id": 7, "topic_id": 5}, {"chat_id": "-100", "message_id": 8}
    requests = {}

    def guard(args):
        return _guard(data, "telegram-bot", "telegram_operation",
                      {"method": "setMessageReaction", "reaction": thumb, "request_id": "guard", **args})

    def queue(request_id, args):
        answer = operate(method="setMessageReaction", reaction=thumb, request_id=request_id, **args)
        assert answer["state"] == "queued", answer
        requests[request_id] = f"telegram-operation:{request_id}"

    def phase(mark):
        counts = {}
        for message_id in calls:
            counts[message_id] = counts.get(message_id, 0) + 1
        return {"receipts": {key: runtime.store.delivery_receipt(value)["state"] for key, value in requests.items()},
                "calls": counts, "queries": queries[mark:]}

    def everything_due():
        with sqlite3.connect(runtime.store.path) as db:
            db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")

    async def drain():
        for _ in range(4):
            await runtime.process_one_outbox()

    facts = {"guard": {}}

    async def work():
        await host.discover_delivery_support()
        facts["guard"]["topic_active"] = guard(in_topic)
        queue("active-thread", in_topic)
        await drain()
        facts["active"] = phase(0)
        mark = len(queries)
        queue("queued-while-active", in_topic)
        p.edit_place(data, thread.place_id, "set_state", author="owner", state="paused")
        facts["places"] = _places_by_scope(data)
        facts["guard"]["topic_paused"], facts["guard"]["general_paused"] = guard(in_topic), guard(in_general)
        queue("room-only", in_general)
        await drain()
        facts["paused"] = {**phase(mark), "held_error": runtime.store.delivery_receipt(requests["queued-while-active"])["error"]}
        p.edit_place(data, thread.place_id, "set_state", author="owner", state="active")
        everything_due()
        await drain()
        facts["resumed"] = phase(len(queries))
    asyncio.run(work())
    return facts


def _slack_tools(state):
    """This skill's own registered tools, loaded as its package the way its tests load it: ``call(tool_name, **arguments)``."""
    import importlib.util
    import types

    skill = ROOT / "skills" / "slack-bridge"
    package = types.ModuleType("slack_bridge_consumer")
    package.__path__ = [str(skill)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location("slack_bridge_consumer.plugin", skill / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    tools = {}

    class Api:
        def get_state_dir(self):
            return str(state)

        def get_settings(self, keys):
            return {key: "" for key in keys}

        def register_tool(self, name, handler, **_metadata):
            tools[name] = handler

        def __getattr__(self, _name):  # companions, routes, tabs and settings sections are not under test
            return lambda *_args, **_kwargs: None
    module.register(Api())

    def call(tool_name, /, **arguments):
        import asyncio

        result = tools[tool_name](**arguments)  # slack_api is async; it is called here outside any loop
        return json.loads(asyncio.run(result) if asyncio.iscoroutine(result) else result)
    return call


def _slack_writes(writes):
    """Slack's scripted answer for a write, recording it as ``add 1.5`` or ``post <words>``."""
    import httpx

    def provider(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else dict(request.url.params)  # GET: its query
        if method == "chat.postMessage":
            writes.append(f"post {body['markdown_text']}")
            return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": f"{len(writes)}.9"})
        writes.append(f"{method.split('.')[1]} {body['timestamp']}")
        return httpx.Response(200, json={"ok": True})
    return provider


def _paused_thread_slack(root):
    """The same moments for Slack, plus what a held reaction holds back and a thread Paused before queueing."""
    import asyncio
    import sqlite3

    import httpx

    from ouroboros import places as p
    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore

    data = root / "data"
    thread = _migrated_thread(data, connector="slack-bridge", provider="slack", account="T1", conversation="C1", thread="1.0")
    world = _core(root, "slack-bridge", "slack", "T1", "C1")
    state = root / "slack-state"
    store = BridgeStore(state)
    store.set_runtime(workspace_id="T1")

    def message(event_id, ts, **extra):
        return {"type": "events_api", "envelope_id": event_id, "payload": {"event_id": event_id, "team_id": "T1", "event": {
            "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": ts, "text": "words", **extra}}}
    # Retained: the thread's root and a reply in it, and a top-level message that started no thread.
    for envelope in (message("Ev-root", "1.0"), message("Ev-reply", "1.5", thread_ts="1.0"), message("Ev-top", "3.0")):
        store.ingest_envelope(envelope, parse_socket_envelope(envelope, bot_user_id="U_BOT"))
    tool = _slack_tools(state)
    writes, queries, requests = [], [], []
    provider = _slack_writes(writes)

    class Recording(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/presence/place":
                queries.append(dict(request.url.params))
            return await world.transport.handle_async_request(request)

    def guard(ts):
        return _guard(data, "slack-bridge", "slack_reaction_add", {"channel": "C1", "ts": ts, "name": "eyes"})

    def queue(request_id, ts, tool_name="slack_reaction_add"):
        answer = tool(tool_name, channel="C1", ts=ts, name="eyes", request_id=request_id)
        assert answer["state"] == "queued", answer
        requests.append(request_id)

    def phase(mark, keys):
        return {"receipts": {key: store.delivery_receipt(key)["parts"][0]["state"] for key in keys},
                "writes": list(writes), "queries": queries[mark:]}

    def everything_due():
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")

    facts = {"guard": {}}

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=Recording(), timeout=60.0) as host_http:
            adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                                  skill_token=TOKEN, http_client=host_http)
            worker = OutboundWorker(store, SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http), adapter)
            await _discover_reporting(adapter, store)

            async def drain():
                for _ in range(4):
                    await worker.process_once()
                    await asyncio.sleep(0.01)
                await _reported(worker, store)
            facts["guard"]["reply_active"] = guard("1.5")
            queue("active-thread", "1.5")
            await drain()
            facts["active"] = phase(0, ["active-thread"])
            mark = len(queries)
            queue("queued-while-active", "1.5")
            p.edit_place(data, thread.place_id, "set_state", author="owner", state="paused")
            facts["places"] = _places_by_scope(data)
            facts["guard"]["reply_paused"], facts["guard"]["top_paused"] = guard("1.5"), guard("3.0")
            await drain()
            facts["paused"] = {**phase(mark, ["active-thread", "queued-while-active"]),
                               "held_error": store.delivery_receipt("queued-while-active")["parts"][0]["error"]}
            # Behind the held row: a reaction to another message, a top-level send and the same message's remove.
            queue("room-behind-hold", "3.0")
            sent = tool("slack_send", channel_or_user="C1", text="independent", request_id="top-level-send")
            assert sent["state"] == "queued", sent
            queue("same-message-remove", "1.5", tool_name="slack_reaction_remove")
            everything_due()
            await drain()
            behind = phase(len(queries), ["queued-while-active", "room-behind-hold", "top-level-send", "same-message-remove"])
            facts["behind_hold"] = {"receipts": behind["receipts"], "writes": behind["writes"],
                                    "remove_attempts": store.delivery_receipt("same-message-remove")["parts"][0]["attempts"],
                                    "send_report": store.delivery_receipt("top-level-send")["parts"][0]["history_report_state"]}
            p.edit_place(data, thread.place_id, "set_state", author="owner", state="active")
            everything_due()
            await drain()
            resumed = phase(len(queries), ["queued-while-active", "room-behind-hold", "same-message-remove"])
            facts["resumed"] = {"receipts": resumed["receipts"], "writes": resumed["writes"]}
            p.edit_place(data, thread.place_id, "set_state", author="owner", state="paused")
            mark = len(queries)
            queue("room-retained", "3.0")
            queue("room-unretained", "9.0")
            queue("queued-while-paused", "1.5")
            await drain()
            facts["paused_again"] = phase(mark, ["room-retained", "room-unretained", "queued-while-paused"])
            await worker.aclose()
    asyncio.run(work())
    return facts


def _slack_lanes_upgrade(root):
    """Rows the previous version queued in its channel's lane, an older writer, a late target and a restart.

    The migrated thread is Paused under an Active channel throughout, until the owner resumes it.
    """
    import asyncio
    import sqlite3
    import time

    import httpx

    from ouroboros import places as p
    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore

    data = root / "data"
    thread = _migrated_thread(data, connector="slack-bridge", provider="slack", account="T1", conversation="C1", thread="1.0")
    world = _core(root, "slack-bridge", "slack", "T1", "C1")
    p.edit_place(data, thread.place_id, "set_state", author="owner", state="paused")
    state = root / "slack-state"
    earlier = BridgeStore(state)
    earlier.set_runtime(workspace_id="T1", presence_delivery_version=1)  # what the running bridge had discovered

    def ingest(store, event_id, ts, **extra):
        envelope = {"type": "events_api", "envelope_id": event_id, "payload": {"event_id": event_id, "team_id": "T1", "event": {
            "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": ts, "text": "words", **extra}}}
        store.ingest_envelope(envelope, parse_socket_envelope(envelope, bot_user_id="U_BOT"))
    ingest(earlier, "Ev-root", "1.0")
    ingest(earlier, "Ev-reply", "1.5", thread_ts="1.0")
    ingest(earlier, "Ev-top", "3.0")

    def earlier_row(request_id, ts, operation="reaction_add", *, channel="C1", payload=None, **columns):
        """A reaction row exactly as the previous version queued it: raw arguments, in its channel's lane."""
        with sqlite3.connect(earlier.path) as db:
            db.execute(
                """INSERT INTO outbox(request_id,chunk_index,chunk_count,target,thread_ts,text,ordering_key,kind,operation,
                                      payload_json,origin_json,delivery_reporting_version,created_at,updated_at)
                   VALUES(?,0,1,?,'','',?,'mutation',?,?,'{"kind":"tool"}',1,1,1)""",
                (request_id, channel, f"{channel}:", operation,
                 json.dumps(payload or {"channel": channel, "ts": ts, "name": "eyes"})))
            for name, value in columns.items():
                db.execute(f"UPDATE outbox SET {name}=? WHERE request_id=?", (value, request_id))
    now = time.time()
    earlier_row("earlier-held", "1.5", last_error="Held: Slack is paused")
    earlier_row("earlier-in-flight", "3.0", state="leased", lease_token="older-worker", lease_until=now + 600,
                send_started=1, attempts=1)
    earlier_row("earlier-crashed", "9.0", state="leased", lease_token="gone", lease_until=now - 1, send_started=1, attempts=1)
    earlier_row("earlier-same-message", " 1.5 ", "reaction_remove", channel=" C1 ")

    def api(method, path, ts):
        """A slack_api reactions write as the previous version's tool stored it."""
        arguments = {"channel": "C1", "timestamp": ts, "name": "eyes"}
        return {"method": method, "path": path, "effect": "write", "result_kind": "auto",
                "params": arguments if method == "GET" else {}, "body": arguments if method == "POST" else {}}
    earlier_row("earlier-api-in-flight", "", "generic_api", payload=api("POST", "reactions.add", "5.0"), state="leased",
                lease_token="older-api-worker", lease_until=now + 600, send_started=1, attempts=1)
    earlier.enqueue_outbox(request_id="earlier-text", target="C1", thread_ts="", chunks=["earlier words"],
                           origin={"kind": "tool"}, delivery_reporting_version=1)

    def lanes(store):
        with sqlite3.connect(store.path) as db:
            return dict(db.execute("SELECT request_id,ordering_key FROM outbox"))
    facts = {"lanes_before": lanes(earlier)}
    store = BridgeStore(state)  # the updated bridge opens it
    facts["lanes_opened"] = lanes(store)
    tool = _slack_tools(state)

    def queued(answer):
        assert answer["state"] == "queued", answer
    queued(tool("slack_send", channel_or_user="C1", text="after upgrade", request_id="after-upgrade"))
    queued(tool("slack_reaction_remove", channel="C1", ts="3.0", name="eyes", request_id="same-as-in-flight"))
    queued(tool("slack_reaction_add", channel="C1", ts="7.0", name="eyes", request_id="target-unknown"))
    ingest(store, "Ev-late", "7.0", thread_ts="1.0")  # the target's record arrives after the add was queued
    queued(tool("slack_reaction_remove", channel="C1", ts="7.0", name="eyes", request_id="target-known"))
    queued(tool("slack_api", method="POST", path="reactions.remove", request_id="api-same-as-in-flight",
                body={"channel": "C1", "timestamp": "5.0", "name": "eyes"}))
    queued(tool("slack_api", method="GET", path="reactions.add", request_id="api-get-independent",
                params={"channel": "C1", "timestamp": "9.5", "name": "eyes"}))
    # Previous-version processes, after every open: a dedicated remove and a slack_api one, both for the held message.
    earlier_row("older-writer", " 1.5 ", "reaction_remove", channel=" C1 ")
    earlier_row("older-api-writer", "", "generic_api", payload=api("GET", "reactions.remove", "1.5"))
    keys = ("earlier-held", "earlier-in-flight", "earlier-crashed", "earlier-same-message", "earlier-api-in-flight",
            "earlier-text", "after-upgrade", "same-as-in-flight", "target-unknown", "target-known",
            "api-same-as-in-flight", "api-get-independent", "older-writer", "older-api-writer")
    writes = []
    provider = _slack_writes(writes)

    def phase(store):
        parts = {key: store.delivery_receipt(key)["parts"][0] for key in keys}
        return {"receipts": {key: part["state"] for key, part in parts.items()}, "writes": list(writes),
                "attempts": {key: part["attempts"] for key, part in parts.items()},
                "reports": {key: part["history_report_state"] for key, part in parts.items()},
                "errors": {key: part["error"] for key, part in parts.items() if part["error"]}}

    def everything_due(store):
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")

    async def work():
        nonlocal store
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=world.transport, timeout=60.0) as host_http:
            async def started(store):
                adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                                      skill_token=TOKEN, http_client=host_http)
                worker = OutboundWorker(store, SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http), adapter)
                await _discover_reporting(adapter, store)
                return worker

            async def drain(worker, store):
                for _ in range(8):
                    await worker.process_once()
                    await asyncio.sleep(0.01)
                await _reported(worker, store)
            worker = await started(store)
            await drain(worker, store)
            facts["paused"] = {**phase(store), "lanes": lanes(store)}
            # The older workers' own attempts end under their own tokens; only then does each message's next write go.
            for request_id, token in (("earlier-in-flight", "older-worker"), ("earlier-api-in-flight", "older-api-worker")):
                with sqlite3.connect(store.path) as db:
                    in_flight = db.execute("SELECT id FROM outbox WHERE request_id=?", (request_id,)).fetchone()[0]
                store.complete_outbox(in_flight, token, result={"ok": True})
            await drain(worker, store)
            facts["in_flight_done"] = phase(store)
            # A restart while rows are held: a new process opens the store, a new worker carries on.
            await worker.aclose()
            store = BridgeStore(state)
            worker = await started(store)
            everything_due(store)
            await drain(worker, store)
            facts["restarted"] = phase(store)
            p.edit_place(data, thread.place_id, "set_state", author="owner", state="active")
            everything_due(store)
            await drain(worker, store)
            facts["resumed"] = phase(store)
            await worker.aclose()
    asyncio.run(work())
    return facts


def _drive(root: Path, mode: str) -> dict:
    if mode.startswith("server:"):
        _phase, token, *revoked = mode.split(":")
        return _server_phase(root, token, bool(revoked))
    if mode.startswith("worker:"):
        return _worker_phase(root, mode.split(":")[1])
    if mode == "reads":
        return _reads(root)
    if mode == "telegram-known":
        return _telegram_known(root)
    if mode == "slack-padded":
        return _slack_padded(root)
    if mode == "paused-thread-telegram":
        return _paused_thread_telegram(root)
    if mode == "paused-thread-slack":
        return _paused_thread_slack(root)
    if mode == "slack-lanes-upgrade":
        return _slack_lanes_upgrade(root)
    if mode == "frozen":
        return _frozen(root)
    if mode == "frozen-held":
        return _frozen(root, host=OLD_HOST)
    if mode == "frozen-explicit":
        return _frozen(root, host=EXPLICIT_ONLY_HOST)
    if mode == "slack":
        world, store, inbox = _slack(root)
        rows, reasons, actions, candidates = _readback(world)
        receipts = {key: store.delivery_receipt(key)["parts"][0]["state"] for key in ("react-ok", "react-noop", "react-gone")}
        return {"statuses": world.statuses, "refusals": world.refusals, "woken": world.woken, "rows": rows, "reasons": reasons, "actions": actions,
                "inbox": inbox, "receipts": receipts, "recorded": world.recorded,
                "candidates": sorted(row["platform_actor_id"] for row in candidates(world.data, _place(world.data, "slack")))}
    if mode == "telegram":
        world, runtime = _telegram(root)
        rows, reasons, actions, candidates = _readback(world)
        receipts = {key: runtime.store.delivery_receipt(f"telegram-operation:{key}")["state"] for key in ("set", "refused")}
        return {"woken": world.woken, "rows": rows, "reasons": reasons, "actions": actions, "receipts": receipts,
                "recorded": world.recorded,
                "candidates": sorted(row["platform_actor_id"] for row in candidates(world.data, _place(world.data, "telegram")))}
    host = OLD_HOST if mode == "old-host" else EXPLICIT_ONLY_HOST
    slack_world, slack_store, _ = _slack(root / "s" if (root / "s").mkdir() is None else root, host=host)
    telegram_world, runtime = _telegram(root / "t" if (root / "t").mkdir() is None else root, host=host)
    return {
        "core_identity": telegram_world.core_identity, "identity": telegram_world.identity,
        "slack_turns": slack_world.turns, "telegram_turns": telegram_world.turns,
        "slack_withheld": slack_store.status()["reactions_withheld"],
        "telegram_withheld": runtime.store.status_snapshot()["reactions_withheld"],
        "reports": slack_world.reports + telegram_world.reports,
        "recorded": slack_world.recorded + telegram_world.recorded,
        "actions": _readback(slack_world)[2] + _readback(telegram_world)[2],
        "slack_applied": slack_store.delivery_receipt("react-ok")["parts"][0]["provider_result"]["effect"],
        "telegram_applied": runtime.store.delivery_receipt("telegram-operation:set")["provider_receipt"]["effect"],
    }


def _place(data, provider):
    from ouroboros import places as p
    return next(place.place_id for place in p.load_places(data) if place.provider == provider)


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, default=str))
