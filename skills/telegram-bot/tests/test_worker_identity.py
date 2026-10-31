"""Reaction targets from a pooled worker, through the real ``plugin.register``.

Core registers a plugin in every pooled worker but starts its supervised task only in the server
process, so the ``telegram_operation`` a task calls usually belongs to a runtime that never runs.
Each instance here is ``plugin.py`` loaded as a module of its own, as each process has, on a shared
state directory. A synthetic Bot API answers the unchanged ``TelegramClient`` of the server
instance; the worker instance calls nothing. ``tests/test_reaction_core_consumer.py`` repeats this
through core's real loader and dispatch when a core is available.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from telegram_bot.custody import CustodyStore
from telegram_bot.events import parse_telegram_update
from test_group_discovery import BotApi, UnconnectedHost, _brief, _plugin, chat, said, until
from test_plugin import FakeApi

THUMB = {"type": "emoji", "emoji": "👍"}
GROUP = chat(-200, "Plain")
FORUM = {**chat(-100, "Forum"), "is_forum": True}
DM_WITH_TOPICS = chat(55, kind="private")
DM = chat(56, kind="private")
UPDATES = [
    said(1, GROUP),
    said(2, FORUM, message_thread_id=5, is_topic_message=True),
    said(3, FORUM),
    said(4, DM_WITH_TOPICS, message_thread_id=9, is_topic_message=True),
    said(5, DM),
]


class PluginApi(FakeApi):
    """The plugin API of one process, reading one settings document that every process shares."""

    def __init__(self, state_dir, settings):
        super().__init__(state_dir)
        self.document, self.logged = settings, []

    def get_settings(self, keys):
        assert keys == ["TELEGRAM_PUBLIC_BOT_TOKEN"]
        return {"TELEGRAM_PUBLIC_BOT_TOKEN": self.document["token"]}

    def log(self, level, message):
        self.logged.append((level, message))


def instance(state_dir, settings, plugin=None):
    plugin = plugin or _plugin()
    api = PluginApi(state_dir, settings)
    plugin.register(api)
    operate = next(handler for name, handler, _meta in api.tools if name == "telegram_operation")
    return SimpleNamespace(plugin=plugin, api=api, runtime=plugin._RUNTIME, operate=operate)


def answered_by(runtime, bots):
    """This instance's own ``TelegramClient`` over the synthetic Bot API; the Host is a fake."""
    client = runtime.client_factory
    runtime.client_factory = lambda token: client(token, transport=bots, api_server="http://telegram.invalid")
    runtime.submitter = UnconnectedHost()


class Server:
    """``plugin.register``, then the supervised task it declared, as core starts it in the server process."""

    def __init__(self, state_dir, settings, bots):
        self.parts = instance(state_dir, settings)
        self.runtime, self.operate = self.parts.runtime, self.parts.operate
        answered_by(self.runtime, bots)
        self.runtime._pause = _brief
        self.task = None

    async def __aenter__(self):
        [(_name, supervised, _options)] = self.parts.api.tasks
        self.task = asyncio.create_task(supervised())
        await asyncio.sleep(0)  # run() clears a stop that arrives before it begins, which would hang the exit
        return self

    async def __aexit__(self, *_exc):
        self.parts.api.unload()
        await asyncio.gather(self.task, return_exceptions=True)


def bots_with_history():
    bots = BotApi()
    bots.updates["token-a"] = list(UPDATES)
    bots.bots["token-b"] = {"id": 77, "is_bot": True, "username": "other_bot"}
    return bots


async def confirmed(server):
    await until(lambda: server.runtime.recorded_bot() == "9")
    await until(lambda: server.runtime.store.telegram_offset() > len(UPDATES))


_REQUESTS = itertools.count(1)


def react(operate, chat_id, message_id, topic=None):
    request = f"request-{next(_REQUESTS)}"
    answer = operate(method="setMessageReaction", chat_id=str(chat_id), message_id=message_id, reaction=[THUMB],
                     request_id=request, **({"topic_id": topic} if topic else {}))
    return request, answer


def queued_target(state_dir, request):
    payload = CustodyStore(Path(state_dir) / "custody.sqlite3").outbox_payload(f"telegram-operation:{request}")
    return payload["_reaction_target"]["resolution"], payload.get("topic_id")


# message ids are the update ids of UPDATES: 1 in the plain group, 2 in forum topic 5, 3 in the forum's
# General, 4 in a private chat's topic 9, 5 in a private chat with no topic.
WHAT_THE_RECORDS_SAY = [
    ("retained plain group", -200, 1, None, ("resolved", None)),
    ("retained plain group, a topic it has none of", -200, 1, 3, "forum_topic_mismatch"),
    ("unretained in a known plain group", -200, 999, None, ("resolved", None)),
    ("retained forum message, its topic", -100, 2, 5, ("resolved", "5")),
    ("retained forum message, a wrong topic", -100, 2, 6, "forum_topic_mismatch"),
    ("retained forum message, no topic", -100, 2, None, "topic_id=5"),
    ("retained in the forum's General", -100, 3, None, ("resolved", None)),
    ("unretained in a known forum, no topic", -100, 999, None, "is a forum"),
    ("unretained in a known forum, a given topic", -100, 999, 6, ("unresolved", "6")),
    ("retained in a private topic, its topic", 55, 4, 9, ("resolved", "9")),
    ("retained in a private topic, no topic", 55, 4, None, "topic_id=9"),
    ("unretained in a private chat that showed topics", 55, 999, None, "has shown topics"),
    ("private chat with no topics", 56, 5, None, ("resolved", None)),
    ("a group never observed", -300, 1, None, "not observed yet"),
]


def test_a_worker_answers_every_target_as_the_running_server_does(tmp_path):
    async def run():
        settings = {"token": "token-a"}
        async with Server(tmp_path, settings, bots_with_history()) as server:
            await confirmed(server)
            worker = instance(tmp_path, settings)
            # The split itself: this instance registered everything and its runtime never ran.
            assert worker.runtime._run_task is None and worker.runtime is not server.runtime
            assert worker.runtime.confirmed_bot() == ("", "Telegram bot not running")
            for label, chat_id, message_id, topic, expected in WHAT_THE_RECORDS_SAY:
                for who, operate in (("worker", worker.operate), ("server", server.operate)):
                    request, answer = react(operate, chat_id, message_id, topic)
                    if isinstance(expected, tuple):
                        assert answer.get("state") == "queued", (who, label, answer)
                        assert queued_target(tmp_path, request) == expected, (who, label)
                    else:
                        assert answer["ok"] is False and expected in answer["error"], (who, label, answer)
                        assert "unconfirmed" not in answer["error"]
    asyncio.run(run())


def test_a_worker_in_another_process_reads_what_the_server_confirmed(tmp_path):
    script = """
import importlib.util, json, sys
skill, tests, state = sys.argv[1:4]
sys.path[:0] = [skill, tests]
from test_plugin import FakeApi
spec = importlib.util.spec_from_file_location("worker_process_plugin", skill + "/plugin.py", submodule_search_locations=[skill])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
api = FakeApi(state)
api.get_settings = lambda _keys: {"TELEGRAM_PUBLIC_BOT_TOKEN": "token-a"}
plugin.register(api)
operate = next(handler for name, handler, _meta in api.tools if name == "telegram_operation")
base = dict(method="setMessageReaction", reaction=[{"type": "emoji", "emoji": "x"}])
print(json.dumps({
    "never_started": plugin._RUNTIME._run_task is None, "confirmed": plugin._RUNTIME.confirmed_bot(),
    "plain": operate(chat_id="-200", message_id=1, request_id="plain", **base),
    "wrong": operate(chat_id="-100", message_id=2, topic_id=6, request_id="wrong", **base),
}))
"""
    async def serve():
        async with Server(tmp_path, {"token": "token-a"}, bots_with_history()) as server:
            await confirmed(server)
    asyncio.run(serve())
    here = Path(__file__).resolve().parent
    run = subprocess.run([sys.executable, "-c", script, str(here.parent), str(here), str(tmp_path)],
                         capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert run.returncode == 0, run.stderr
    facts = json.loads(run.stdout.strip().splitlines()[-1])
    assert facts["never_started"] is True and facts["confirmed"] == ["", "Telegram bot not running"]
    assert facts["plain"]["state"] == "queued"
    assert "forum_topic_mismatch" in facts["wrong"]["error"]


def assert_unconfirmed(operate, state_dir):
    """Nothing retained can be read as this bot's: the refusal says so, and what stays queued claims nothing."""
    for chat_id, message_id in ((-200, 1), (-100, 2), (-300, 1)):
        _request, refused = react(operate, chat_id, message_id)
        assert refused["ok"] is False and "forum_topic_unknown" in refused["error"], refused
        assert "identity is unconfirmed" in refused["error"], refused
        assert "not retained here" not in refused["error"] and "not observed yet" not in refused["error"]
    for chat_id, message_id, topic, expected in ((-100, 2, 6, ("unresolved", "6")), (55, 4, None, ("unresolved", None)),
                                                 (55, 4, 9, ("unresolved", "9")), (56, 5, None, ("unresolved", None))):
        request, answer = react(operate, chat_id, message_id, topic)
        assert answer.get("state") == "queued", answer
        assert queued_target(state_dir, request) == expected


def test_a_never_confirmed_or_missing_token_reads_no_record(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    for update in UPDATES:
        store.commit_update(update["update_id"], parse_telegram_update(update, bot_account_id="9", places=True))
    settings = {"token": "token-a"}
    worker = instance(tmp_path, settings)
    assert worker.runtime.recorded_bot() == ""
    assert_unconfirmed(worker.operate, tmp_path)
    settings["token"] = ""
    assert_unconfirmed(worker.operate, tmp_path)


def test_a_changed_token_is_another_bot_until_getme_answers_for_it(tmp_path):
    async def run():
        settings = {"token": "token-a"}
        bots = bots_with_history()
        bots.updates["token-b"] = [said(6, GROUP)]
        async with Server(tmp_path, settings, bots) as server:
            await confirmed(server)
            worker = instance(tmp_path, settings)
            assert react(worker.operate, -200, 1)[1]["state"] == "queued"
            bots.open.clear()
            await until(lambda: bots.waiting == 1)  # getUpdates of the earlier bot is held, nothing moves
            settings["token"] = "token-b"
            # Neither process reuses the earlier bot, whose record is still there.
            assert worker.runtime.recorded_bot() == "" and server.runtime.recorded_bot() == ""
            assert server.runtime.confirmed_bot() == ("", "Bot identity is unconfirmed; check the transport status")
            assert_unconfirmed(worker.operate, tmp_path)
            assert_unconfirmed(server.operate, tmp_path)
            bots.open.set()
            await until(lambda: worker.runtime.recorded_bot() == "77")
            await until(lambda: server.runtime.store.telegram_offset() >= 7)
            # Now the other bot, whose records hold only its own message.
            assert react(worker.operate, -200, 1)[1]["state"] == "queued"  # bot 77 saw that group: no topics
            assert "not observed yet" in react(worker.operate, -100, 2)[1]["error"]
            settings["token"] = "token-a"
            await until(lambda: worker.runtime.recorded_bot() == "9")
            assert "forum_topic_mismatch" in react(worker.operate, -100, 2, 6)[1]["error"]
            assert server.runtime.confirmed_bot()[0] == "9"
    asyncio.run(run())


def test_a_refused_token_is_not_confirmed_and_a_refusal_of_another_keeps_the_record(tmp_path):
    async def run():
        settings = {"token": "token-a"}
        bots = bots_with_history()
        async with Server(tmp_path, settings, bots) as server:
            await confirmed(server)
            worker = instance(tmp_path, settings)
            getme = len(bots.calls("getMe"))
            settings["token"] = "token-refused"
            await until(lambda: any(token == "token-refused" for token, *_rest in bots.calls("getMe")))
            await until(lambda: server.runtime.status_snapshot()["runtime_state"] == "provider_error")
            assert worker.runtime.recorded_bot() == ""
            assert_unconfirmed(worker.operate, tmp_path)
            # Back on the token the running client still holds: no new getMe, and that bot still answers everywhere.
            refused_calls = len(bots.calls("getMe"))
            settings["token"] = "token-a"
            await until(lambda: server.runtime.status_snapshot()["runtime_state"] == "polling")
            assert refused_calls >= getme and all(token != "token-a" for token, *_rest in bots.calls("getMe")[getme:])
            assert worker.runtime.recorded_bot() == "9" and server.runtime.confirmed_bot()[0] == "9"
            assert "forum_topic_mismatch" in react(worker.operate, -100, 2, 6)[1]["error"]
    asyncio.run(run())


@pytest.mark.parametrize("code,endpoint", [(401, "getMe"), (401, "deleteWebhook"), (404, "getMe")])
def test_telegram_refusing_the_confirmed_token_ends_its_confirmation_until_it_answers_again(tmp_path, code, endpoint):
    async def run():
        class Refusing(BotApi):
            refusing = False

            async def post_json(self, url, payload, timeout_sec):
                if self.refusing and url.endswith("/" + endpoint):
                    return {"ok": False, "error_code": code, "description": "Unauthorized"}
                return await super().post_json(url, payload, timeout_sec)
        settings = {"token": "token-a"}
        bots = Refusing()
        bots.updates["token-a"] = list(UPDATES)
        async with Server(tmp_path, settings, bots) as server:
            await confirmed(server)
        worker = instance(tmp_path, settings)
        assert worker.runtime.recorded_bot() == "9"  # between runs the last answer stands
        bots.refusing = True
        async with Server(tmp_path, settings, bots) as restarted:
            await until(lambda: worker.runtime.recorded_bot() == "")
            assert restarted.runtime.confirmed_bot() == ("", "Bot identity is unconfirmed; check the transport status")
            assert_unconfirmed(worker.operate, tmp_path)
            bots.refusing = False  # the token works again, so getMe, asked again, confirms it
            await until(lambda: worker.runtime.recorded_bot() == "9")
            assert "forum_topic_mismatch" in react(worker.operate, -100, 2, 6)[1]["error"]
    asyncio.run(run())


@pytest.mark.parametrize("answer", [{"ok": False, "error_code": 429, "description": "Too Many Requests"},
                                    {"ok": False, "error_code": 502, "description": "Bad Gateway"},
                                    ConnectionError("no route")], ids=["429", "502", "no-answer"])
def test_a_getme_failure_that_does_not_refuse_the_token_keeps_its_last_answer(tmp_path, answer):
    async def run():
        settings = {"token": "token-a"}
        bots = bots_with_history()
        async with Server(tmp_path, settings, bots) as server:
            await confirmed(server)

        class Down(BotApi):
            calls_seen = 0

            async def post_json(self, url, payload, timeout_sec):
                if url.endswith("/getMe"):
                    Down.calls_seen += 1
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
                return await super().post_json(url, payload, timeout_sec)
        worker = instance(tmp_path, settings)
        async with Server(tmp_path, settings, Down()) as restarted:
            await until(lambda: Down.calls_seen >= 2)
            assert restarted.runtime.confirmed_bot() == ("", "Bot identity is unconfirmed; check the transport status")
            # The same token cannot have become another bot, and no answer said it stopped being this one.
            assert worker.runtime.recorded_bot() == "9"
            assert "forum_topic_mismatch" in react(worker.operate, -100, 2, 6)[1]["error"]
    asyncio.run(run())


def test_an_answer_without_a_usable_id_confirms_nothing(tmp_path):
    async def run():
        settings = {"token": "token-a"}
        bots = bots_with_history()
        async with Server(tmp_path, settings, bots) as server:
            await confirmed(server)
        worker = instance(tmp_path, settings)
        bots.bots["token-a"] = {"is_bot": True, "username": "no_id_bot"}
        async with Server(tmp_path, settings, bots) as restarted:
            await until(lambda: restarted.runtime.status_snapshot()["runtime_state"] == "polling")
            assert restarted.runtime.confirmed_bot()[0] == "" and worker.runtime.recorded_bot() == ""
            assert_unconfirmed(worker.operate, tmp_path)
    asyncio.run(run())


def test_a_failed_record_leaves_no_new_identity_and_getme_is_asked_again(tmp_path):
    async def run():
        settings = {"token": "token-a"}
        bots = bots_with_history()
        worker = instance(tmp_path, settings)
        runtime = worker.runtime
        answered_by(runtime, bots)
        real, failed = runtime.store.record_confirmed_bot, []

        def fails_once(token, account):
            if not failed:
                failed.append(account)
                raise sqlite3.OperationalError("database is locked")
            real(token, account)
        runtime.store.record_confirmed_bot = fails_once
        with pytest.raises(sqlite3.OperationalError):
            await runtime._ready_client()
        assert (runtime._bot_id, runtime._client_token, runtime._client) == ("", "", None) and runtime.recorded_bot() == ""
        assert await runtime._ready_client() is not None
        assert runtime._bot_id == "9" and runtime.recorded_bot() == "9" and len(bots.calls("getMe")) == 2
    asyncio.run(run())


def test_the_status_route_of_a_process_that_does_not_poll_keeps_saying_so(tmp_path):
    async def run():
        settings = {"token": "token-a"}
        async with Server(tmp_path, settings, bots_with_history()) as server:
            await confirmed(server)
            assert server.runtime.confirmed_bot() == ("9", "")
            worker = instance(tmp_path, settings)
            payload = worker.plugin._status_payload()
            assert worker.runtime.recorded_bot() == "9"
            assert worker.runtime.confirmed_bot() == ("", "Telegram bot not running")
            assert payload["connect"] is None and payload["connect_unavailable"] == "Telegram bot not running"
            assert payload["runtime_state"] == "not_started" and payload["bot_label"] == "not connected"
        assert server.runtime.confirmed_bot() == ("", "Telegram bot not running")
    asyncio.run(run())


def test_each_instance_reads_its_own_state_and_token(tmp_path):
    async def run():
        here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
        settings = {"token": "token-a"}
        async with Server(here, settings, bots_with_history()) as server:
            await confirmed(server)
        # The same token on another state directory: that one has confirmed nothing.
        other_state = instance(elsewhere, settings)
        assert other_state.runtime.recorded_bot() == "" and instance(here, settings).runtime.recorded_bot() == "9"
        # A second register in one module replaces its _RUNTIME; the first tool keeps its own state and token.
        plugin = _plugin()
        first = instance(here, settings, plugin)
        second = instance(elsewhere, {"token": "token-b"}, plugin)
        assert plugin._RUNTIME is second.runtime
        assert "forum_topic_mismatch" in react(first.operate, -100, 2, 6)[1]["error"]
        assert "identity is unconfirmed" in react(second.operate, -100, 2)[1]["error"]
    asyncio.run(run())


def test_the_token_is_kept_out_of_custody_status_and_logs(tmp_path):
    async def run():
        secret = "123456:AAH-fixture-secret-not-a-real-token"
        settings = {"token": secret}
        bots = bots_with_history()
        bots.bots[secret] = bots.bots["token-a"]
        async with Server(tmp_path, settings, bots) as server:
            await until(lambda: server.runtime.recorded_bot() == "9")
            logged = server.parts.api.logged
            status = server.parts.plugin._status_payload()
            with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
                [(value,)] = conn.execute("SELECT value FROM metadata WHERE key='confirmed_bot'")
        digest, _, account = value.partition(":")
        assert account == "9" and len(digest) == 64 and "AAH" not in value
        files = [path.read_bytes() for path in tmp_path.iterdir() if path.is_file()]
        assert not any(secret.encode() in content or b"AAH-fixture" in content for content in files)
        visible = json.dumps([status, logged], default=str)
        assert secret not in visible and digest not in visible
    asyncio.run(run())
