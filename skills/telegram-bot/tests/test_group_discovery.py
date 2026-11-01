"""Groups the bot was seen in, offered to Connect through the real poll loop and status route.

A synthetic Bot API answers the unchanged ``TelegramClient``: getMe per token and
getUpdates per token, honouring ``offset`` and ``limit``. The runtime, custody store
and plugin status route run as in production; nothing leaves the process.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import itertools
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from telegram_bot import discovery
from telegram_bot.api import TelegramClient
from telegram_bot.custody import CustodyStore
from telegram_bot.discovery import group_sightings
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostHTTPError
from telegram_bot.runtime import TelegramTransportRuntime
from test_runtime import Logger

BOT, OTHER_BOT = 9, 77
NOT_CONNECTED = {"ok": False, "error": "Connect this place in Consciousness > Places.",
                 "code": "place_not_connected", "field": "place", "disposition": "blocked"}
_PLUGINS = itertools.count()


def chat(chat_id, title="Room", kind="supergroup"):
    return {"id": chat_id, "type": kind, **({"first_name": "Ann"} if kind == "private" else {"title": title})}


def said(update_id, room, text="hello", **extra):
    """A message in ``room``; without text it is a service message."""
    message = {"message_id": update_id, "date": 1_700_000_000 + update_id, "chat": room,
               "from": {"id": 3, "is_bot": False, "first_name": "Ann"}, **extra}
    if text:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def member(update_id, room, status, *, bot=BOT, **fields):
    """The bot's own ``my_chat_member`` update in ``room``."""
    return {"update_id": update_id, "my_chat_member": {
        "chat": room, "from": {"id": 3, "is_bot": False}, "date": 1_700_000_000 + update_id,
        "old_chat_member": {"status": "left", "user": {"id": bot, "is_bot": True}},
        "new_chat_member": {"status": status, "user": {"id": bot, "is_bot": True}, **fields}}}


def commit(store, *updates, account=str(BOT)):
    """What the poll loop commits for each raw update."""
    for update in updates:
        event = parse_telegram_update(update, bot_account_id=account, places=True)
        store.commit_update(update["update_id"], event, group_sightings(update, account))


def listed(store, account=str(BOT)):
    return [row["label"] for row in store.offered_groups(account)]


def labels(status):
    return [row["label"] for row in status["connect"]]


async def until(predicate, timeout=30.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


class BotApi:
    """Telegram's Bot API under the real ``TelegramClient``; every request is recorded."""

    def __init__(self):
        self.bots = {"token-a": {"id": BOT, "is_bot": True, "username": "presence_bot"}}
        self.updates = {}
        self.requests = []
        self.open = asyncio.Event()
        self.open.set()
        self.waiting = 0

    def client(self, token):
        return TelegramClient(token, transport=self, api_server="http://telegram.invalid")

    async def post_json(self, url, payload, timeout_sec):
        del timeout_sec
        token, method = url.split("/bot", 1)[1].rsplit("/", 1)
        self.requests.append((token, method, copy.deepcopy(payload)))
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        if method == "getMe":
            bot = self.bots.get(token)
            return {"ok": True, "result": bot} if bot else {"ok": False, "error_code": 401,
                                                            "description": "Unauthorized"}
        assert method == "getUpdates", method
        self.waiting += 1
        try:
            await self.open.wait()
        finally:
            self.waiting -= 1
        await asyncio.sleep(0.005)
        due = [item for item in self.updates.get(token, []) if item["update_id"] >= payload["offset"]]
        return {"ok": True, "result": copy.deepcopy(due[:payload["limit"]])}

    def calls(self, method):
        return [request for request in self.requests if request[1] == method]


class UnconnectedHost:
    """A Places Host where no place covers these chats yet: every turn is refused and kept."""

    places_version = 1

    def __init__(self):
        self.turns = []

    async def submit(self, event, staged_files):
        del staged_files
        self.turns.append(event["source_event_id"])
        raise PresenceHostHTTPError(409, NOT_CONNECTED["error"], NOT_CONNECTED)

    async def poll(self, work_ref, binding_id):
        raise AssertionError("no turn was admitted")


def _plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(f"telegram_group_discovery_{next(_PLUGINS)}",
                                                  root / "plugin.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _brief(seconds):
    # Back-off and token waits are shortened; idle worker pauses stay as they are.
    await asyncio.sleep(0.01 if seconds >= 1 else seconds)


class Connector:
    """The production runtime polling ``api`` from ``root``, read through the plugin's status route."""

    def __init__(self, root, api, token="token-a"):
        self.api, self.token, self.host = api, token, UnconnectedHost()
        self.runtime = TelegramTransportRuntime(
            state_dir=root, token_provider=lambda: self.token, logger=Logger(),
            submitter=self.host, client_factory=api.client)
        self.runtime._pause = _brief
        self.plugin = _plugin()
        self.plugin._RUNTIME, self.plugin._STATE_DIR = self.runtime, Path(root)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.runtime.run())
        return self

    async def __aexit__(self, *_exc):
        self.runtime.stop()
        await asyncio.gather(self.task, return_exceptions=True)

    async def status(self):
        return json.loads((await self.plugin._status_route(None)).body)

    async def settled(self, offset):
        """Wait until the poll loop has committed every update before ``offset``."""
        await until(lambda: self.runtime.store.telegram_offset() >= offset)

    def inbox(self):
        with sqlite3.connect(self.runtime.store.path) as conn:
            return [row[0] for row in conn.execute("SELECT event_id FROM inbox ORDER BY update_id")]


def test_groups_reach_connect_through_the_poll_loop_and_membership_updates_stay_out_of_the_inbox(tmp_path):
    async def run():
        api = BotApi()
        club, twin, renamed = chat(-100, "Book club", "group"), chat(-101, "Book club"), chat(-105, "Old name")
        lab = {**chat(-102, 'R&D <team> "core"\n notes'), "is_forum": True}
        news = chat(-103, "News", "channel")
        api.updates["token-a"] = [
            member(1, club, "member"),                                             # added: no message at all
            said(2, twin, "", new_chat_members=[{"id": BOT, "is_bot": True}]),     # added: a service message
            said(3, club),
            said(4, lab, "topic one", message_thread_id=7, is_topic_message=True),
            said(5, lab, "topic two", message_thread_id=8, is_topic_message=True),
            said(6, chat(3, kind="private"), "private hello"),
            {"update_id": 7, "channel_post": {"message_id": 7, "date": 1, "chat": news, "sender_chat": news,
                                              "text": "post"}},
            member(8, chat(-104, "Announcements", "channel"), "administrator"),
            said(9, renamed),
            said(10, {**renamed, "title": "Renamed"}, "", new_chat_title="Renamed"),
            member(11, twin, "left"),
        ]
        async with Connector(tmp_path, api) as connector:
            await connector.settled(12)
            await until(lambda: len(connector.host.turns) >= 6)
            api.open.clear()  # Telegram holds the next getUpdates: nothing else moves
            await until(lambda: api.waiting == 1)
            requests, turns = len(api.requests), list(connector.host.turns)
            status = await connector.status()
            assert await connector.status() == status
            # The status read is local: no Bot API call, no Host call.
            assert len(api.requests) == requests and connector.host.turns == turns

            assert status["connect"] == [
                {"account_id": "9", "conversation_id": "-100", "label": "Book club (-100)"},
                {"account_id": "9", "conversation_id": "-102", "label": 'R&D <team> "core" notes (-102)'},
                {"account_id": "9", "conversation_id": "-105", "label": "Renamed (-105)"},
            ]
            assert (status["text"], status["tone"]) == ("polling", "muted")
            # The Host's readers treat error/ok:false as a failed read; the bot identity
            # came from getMe although this Host does not advertise presence_identity.
            assert {"error", "ok", "identity", "connect_unavailable"}.isdisjoint(status)
            assert status["telegram_offset"] == 12
            assert status["inbox_waiting"] + status["inbox_leased"] == 6  # refused turns stay queued

            # Additions, removals and renames moved the offset only: no inbox event or turn.
            real = [f"telegram:9:{update_id}" for update_id in (3, 4, 5, 6, 7, 9)]
            assert connector.inbox() == real
            assert set(connector.host.turns) <= set(real)
            with sqlite3.connect(connector.runtime.store.path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM presence_work").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
            assert all("my_chat_member" in request[2]["allowed_updates"] for request in api.calls("getUpdates"))

            # A storage failure leaves the list unavailable, never empty; counts stay visible.
            with sqlite3.connect(connector.runtime.store.path) as conn:
                conn.execute("ALTER TABLE telegram_groups RENAME TO unreadable_groups")
            broken = await connector.status()
            assert broken["connect"] is None and broken["telegram_offset"] == 12
            assert (broken["text"], broken["tone"]) == ("Observed groups could not be read", "warn")
            with sqlite3.connect(connector.runtime.store.path) as conn:
                conn.execute("ALTER TABLE unreadable_groups RENAME TO telegram_groups")
            assert (await connector.status())["connect"] == status["connect"]
            api.open.set()
        stopped = await connector.status()
        assert stopped["connect"] is None and stopped["text"] == "Telegram bot not running"

    asyncio.run(run())


def test_only_the_bot_getme_confirmed_for_the_current_token_offers_its_groups(tmp_path):
    async def run():
        api = BotApi()
        api.updates["token-a"] = [member(1, chat(-100, "Ours"), "member")]
        api.updates["token-b"] = [member(5, chat(-200, "Theirs"), "member", bot=OTHER_BOT)]
        connector = Connector(tmp_path, api)
        idle = await connector.status()
        assert idle["connect"] is None and idle["text"] == "Telegram bot not running"
        async with connector:
            await connector.settled(2)
            assert labels(await connector.status()) == ["Ours (-100)"]

            # While Telegram holds getUpdates the runtime still holds the earlier bot;
            # the status already refuses to offer its groups for another or no token.
            api.open.clear()
            await until(lambda: api.waiting == 1)
            connector.token = ""
            missing = await connector.status()
            assert missing["connect"] is None
            assert (missing["text"], missing["tone"]) == ("Bot token missing", "warn")
            connector.token = "token-b"  # Telegram refuses getMe for it
            assert (await connector.status())["text"] == "Bot identity is unconfirmed; check the transport status"
            assert (await connector.status())["connect"] is None
            api.open.set()
            await until(lambda: any(token == "token-b" for token, *_ in api.calls("getMe")))
            await until(lambda: connector.runtime.status_snapshot()["runtime_state"] == "provider_error")
            refused = await connector.status()
            assert refused["connect"] is None and refused["text"] == "Bot identity is unconfirmed; check the transport status"
            assert refused["last_error"] and refused["runtime_state"] == "provider_error"

            api.bots["token-b"] = {"id": OTHER_BOT, "is_bot": True, "username": "other_bot"}
            await connector.settled(6)
            assert labels(await connector.status()) == ["Theirs (-200)"]
            assert {row["account_id"] for row in (await connector.status())["connect"]} == {"77"}

            connector.token = "token-a"
            await until(lambda: connector.runtime.confirmed_bot()[0] == "9")
            assert labels(await connector.status()) == ["Ours (-100)"]

            connector.token = ""
            await until(lambda: connector.runtime.status_snapshot()["runtime_state"] == "waiting_for_token")
            assert (await connector.status())["connect"] is None

    asyncio.run(run())


def test_updates_fetched_for_the_earlier_bot_stay_its_own_when_a_worker_confirms_a_new_token(tmp_path):
    async def run():
        api = BotApi()
        api.bots["token-b"] = {"id": OTHER_BOT, "is_bot": True, "username": "other_bot"}
        async with Connector(tmp_path, api) as connector:
            await until(lambda: connector.runtime.confirmed_bot()[0] == "9")
            api.open.clear()
            await until(lambda: api.waiting == 1)  # the earlier bot's getUpdates is held
            api.updates["token-a"] = [said(1, chat(-100, "Ours"))]
            connector.token = "token-b"
            await connector.runtime._ready_client()  # as the outbox worker would, first
            assert connector.runtime.confirmed_bot()[0] == "77"
            api.open.set()
            await connector.settled(2)
            assert (await connector.status())["connect"] == []
            assert listed(connector.runtime.store) == ["Ours (-100)"]
            assert connector.inbox() == ["telegram:9:1"]

    asyncio.run(run())


def test_a_confirmed_bot_without_groups_is_known_empty_and_the_list_is_never_capped(tmp_path):
    async def run():
        api = BotApi()
        async with Connector(tmp_path, api) as connector:
            await until(lambda: connector.runtime.confirmed_bot()[0] == "9")
            empty = await connector.status()
            assert empty["connect"] == [] and empty["text"] == "polling"
            # More groups than one getUpdates page, every one of them offered.
            api.updates["token-a"] = [member(1 + index, chat(-1000 - index, f"Group {index:03d}"), "member")
                                      for index in range(150)]
            await connector.settled(151)
            offered = (await connector.status())["connect"]
            assert len(offered) == 150 and offered[0]["label"] == "Group 000 (-1000)"
            assert len({row["conversation_id"] for row in offered}) == 150
            # Telegram's page holds 100 updates; the rest came on the next page.
            assert any(request[2]["offset"] == 101 for request in api.calls("getUpdates"))

    asyncio.run(run())


@pytest.mark.parametrize("status,fields,offered", [
    ("member", {}, True), ("administrator", {}, True), ("creator", {}, True),
    ("restricted", {"is_member": True}, True), ("restricted", {"is_member": False}, False),
    ("left", {}, False), ("kicked", {}, False),
])
def test_the_bots_own_membership_status_decides_its_offer(tmp_path, status, fields, offered):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    room = chat(-100, "Club")
    commit(store, said(1, room), member(2, room, status, **fields))
    assert listed(store) == (["Club (-100)"] if offered else [])
    assert store.claim_inbox().event_id == "telegram:9:1" and store.claim_inbox() is None
    assert store.telegram_offset() == 3


def test_an_older_update_never_undoes_a_removal_and_a_later_one_restores_the_group(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    room = chat(-100, "Club")
    commit(store, said(1, room), member(5, room, "left"))
    # Replays and the legacy backfill carry older update ids.
    commit(store, said(3, room), {"update_id": 4, "edited_message": said(4, room)["message"]})
    commit(store, said(2, room, "", new_chat_members=[{"id": BOT, "is_bot": True}]))
    assert listed(store) == []
    commit(store, member(6, room, "member"))
    assert listed(store) == ["Club (-100)"]
    commit(store, said(7, room, "", left_chat_member={"id": BOT, "is_bot": True}))
    assert listed(store) == []
    commit(store, said(8, room, "", new_chat_members=[{"id": BOT, "is_bot": True}]))
    assert listed(store) == ["Club (-100)"]
    commit(store, member(9, room, "kicked"), said(0, room))
    assert listed(store) == []
    # A later update from the group itself shows the bot there again.
    commit(store, {"update_id": 10, "message_reaction": {"chat": room, "message_id": 3, "user": {"id": 3},
                                                         "date": 1, "old_reaction": [], "new_reaction": []}})
    assert listed(store) == ["Club (-100)"]
    # Someone else leaving is not a removal of the bot.
    commit(store, said(11, room, "", left_chat_member={"id": 3}))
    assert listed(store) == ["Club (-100)"]


def test_a_rename_updates_the_label_and_an_older_title_never_returns(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(1, chat(-100, "First")),
           said(2, chat(-100, "Second"), "", new_chat_title="Second"))
    assert listed(store) == ["Second (-100)"]
    commit(store, said(1, chat(-100, "First")), member(1, chat(-100, "First"), "member"))
    assert listed(store) == ["Second (-100)"]


@pytest.mark.parametrize("order", ["old_group_first", "supergroup_first"])
def test_a_migration_retires_the_old_id_and_offers_the_supergroup_from_its_own_metadata(tmp_path, order):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    old, new = chat(-5, "Club", "group"), chat(-1005, "Club")
    commit(store, said(1, old))
    retire, arrive = said(2, old, "", migrate_to_chat_id=-1005), said(3, new, "", migrate_from_chat_id=-5)
    if order == "old_group_first":
        commit(store, retire)
        # The new id alone has no type or title: it is not offered yet.
        assert listed(store) == []
        commit(store, arrive)
    else:
        commit(store, arrive, retire)
    commit(store, said(1, old))  # an older message cannot bring the old id back
    assert listed(store) == ["Club (-1005)"]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT chat_id, membership FROM telegram_groups ORDER BY chat_id").fetchall() == [
            ("-1005", "present"), ("-5", "migrated")]
    assert store.claim_inbox().event_id == "telegram:9:1" and store.claim_inbox() is None


def test_private_chats_channels_other_members_and_unknown_bots_note_no_group():
    room = chat(-100, "Club")
    assert group_sightings(said(1, chat(3, kind="private")), "9") == ()
    assert group_sightings(said(1, chat(-103, "News", "channel")), "9") == ()
    assert group_sightings(member(1, chat(3, kind="private"), "kicked"), "9") == ()
    assert group_sightings(member(1, chat(-104, "News", "channel"), "administrator"), "9") == ()
    assert group_sightings(member(1, room, "member", bot=OTHER_BOT), "9") == ()
    assert group_sightings(said(1, room), "unknown") == ()
    assert group_sightings(said(1, room), "") == ()
    assert group_sightings({"update_id": 1, "channel_post": said(1, room)["message"]}, "9") == ()
    assert group_sightings(said(1, room), "9") == (discovery.GroupSighting("9", "-100", "present", "supergroup", "Club"),)


@pytest.mark.parametrize("update,expected", [
    (None, ()), ([], ()), ({"update_id": 1}, ()), ({"update_id": 1, "message": []}, ()),
    ({"update_id": 1, "message": {"chat": []}}, ()),
    ({"update_id": 1, "message": {"chat": {"id": [], "type": "group"}}}, ()),
    ({"update_id": 1, "message": {"chat": {"id": -1, "type": ["group"]}}}, ()),
    ({"update_id": 1, "message": {"chat": {"id": -1, "type": "group", "title": 5}, "left_chat_member": "x",
                                  "migrate_to_chat_id": [1], "migrate_from_chat_id": {"a": 1}}},
     (discovery.GroupSighting("9", "-1", "present", "group", ""),)),
    ({"update_id": 1, "my_chat_member": {"chat": {"id": -1, "type": "group"}, "new_chat_member": []}}, ()),
    ({"update_id": 1, "my_chat_member": {"chat": {"id": -1, "type": "group"},
                                         "new_chat_member": {"status": ["member"], "user": {"id": 9}}}}, ()),
    ({"update_id": 1, "message_reaction": {"chat": "x"}}, ()),
])
def test_malformed_updates_never_raise_and_name_only_well_formed_groups(update, expected):
    assert group_sightings(update, "9") == expected


def test_group_notes_commit_in_the_same_transaction_as_the_event_and_offset(tmp_path, monkeypatch):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, said(1, chat(-100, "Club")))

    def unwritable(conn, update_id, sightings):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(discovery, "record_sightings", unwritable)
    with pytest.raises(sqlite3.OperationalError):
        commit(store, said(2, chat(-101, "Other")))
    assert store.telegram_offset() == 2
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT update_id FROM inbox").fetchall() == [(1,)]


def test_the_list_reads_only_its_groups_through_the_account_key(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    commit(store, *[said(index, chat(-100 - index % 3, f"Room {index % 3}")) for index in range(1, 60)])
    statements = []
    connect = store._connect

    @contextmanager
    def traced():
        with connect() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    store._connect = traced
    assert listed(store) == ["Room 0 (-100)", "Room 1 (-101)", "Room 2 (-102)"]
    assert statements and not any(name in sql for sql in statements for name in ("inbox", "outbox", "work"))
    with sqlite3.connect(store.path) as conn:
        plan = " ".join(row[-1] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT chat_id, title FROM telegram_groups "
            "WHERE account_id=? AND membership='present'", ("9",)))
    assert plan.startswith("SEARCH telegram_groups USING INDEX")


# -- Legacy custody: inbox rows saved before this projection existed ---------------------------

_INVALID = {
    "unparsable": "{not json",
    "unknown_bot": {"account_id": "unknown", "conversation_id": "-900",
                    "conversation": {"chat_id": "-900", "chat_type": "group", "title": "Lost"}},
    "no_origin": {"account_id": "9", "conversation_id": "-901"},
    "mismatched": {"account_id": "9", "conversation_id": "-902",
                   "conversation": {"chat_id": "-999", "chat_type": "group", "title": "Odd"}},
    "untyped": {"account_id": "9", "conversation_id": "-903",
                "conversation": {"chat_id": "-903", "chat_type": "unknown", "title": "Untyped"}},
}


def legacy_custody(path, groups=260):
    """A 0.8.0 custody file: the same rows in every state, without the group projection."""
    store = CustodyStore(path)
    for index in range(groups):
        commit_legacy(store, said(index + 1, chat(-1000 - index, f"Group {index:03d}")))
    base = groups + 1
    commit_legacy(store, said(base, chat(3, kind="private")))
    news = chat(-103, "News", "channel")
    commit_legacy(store, {"update_id": base + 1, "channel_post": {
        "message_id": 1, "date": 1, "chat": news, "sender_chat": news, "text": "post"}})
    commit_legacy(store, said(base + 2, chat(-2000, "Theirs")), account=str(OTHER_BOT))
    store.commit_update(base + 3, None)  # an unsupported update moved the offset only
    submitted = store.claim_inbox()
    store.record_submission(submitted.event_id, binding_id="", turn_ref="turn-1", outcome="deferred",
                            text="", work_ref="work-1")
    failed = store.claim_inbox()
    store.mark_inbox_failed(failed.event_id, reason="rejected", receipt={"http_status": 400})
    assert store.claim_inbox(lease_sec=600) is not None  # left leased by a stopped runtime
    store.enqueue_outbox("telegram-send:kept", {"kind": "message", "chat_id": "-1000", "text": "kept"})
    with sqlite3.connect(path) as conn:
        for offset, (name, payload) in enumerate(_INVALID.items()):
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            conn.execute("INSERT INTO inbox(event_id, update_id, conversation_key, payload_json, state, "
                         "attempts, available_at, received_at) VALUES (?, ?, ?, ?, 'pending', 0, 0, 0)",
                         (f"legacy:{name}", base + 10 + offset, f"legacy:{name}", raw))
        conn.execute("DROP TABLE telegram_groups")
        conn.execute("DELETE FROM metadata WHERE key='group_backfill'")


def commit_legacy(store, update, account=str(BOT)):
    """What 0.8.0 committed: the event and the offset, no group notes."""
    store.commit_update(update["update_id"], parse_telegram_update(update, bot_account_id=account, places=True))


def custody_rows(path):
    with sqlite3.connect(path) as conn:
        rows = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("inbox", "outbox", "presence_work")}
        rows["metadata"] = conn.execute(
            "SELECT * FROM metadata WHERE key!='group_backfill' ORDER BY key").fetchall()
    return rows


def backfill_marker(path):
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key='group_backfill'").fetchone()
    return json.loads(row[0]) if row else None


def test_legacy_backfill_offers_every_saved_group_once_and_changes_no_custody_row(tmp_path):
    path = tmp_path / "custody.sqlite3"
    legacy_custody(path)
    before = custody_rows(path)
    assert len(before["inbox"]) == 268 and before["presence_work"] and before["outbox"]

    store = CustodyStore(path)  # the upgrade
    assert custody_rows(path) == before
    offered = listed(store)
    # More rows than one backfill batch, in every inbox state, none left out.
    assert len(offered) == 260 and offered[0] == "Group 000 (-1000)" and offered[-1] == "Group 259 (-1259)"
    assert listed(store, str(OTHER_BOT)) == ["Theirs (-2000)"]
    assert store.status_snapshot()["group_backfill_unreadable"] == len(_INVALID)
    assert backfill_marker(path) == {"after": 275, "done": True, "unreadable": len(_INVALID)}

    # A finished backfill never rescans: a row written behind its back stays unobserved.
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO inbox(event_id, update_id, conversation_key, payload_json, state, attempts, "
                     "available_at, received_at) VALUES ('late', 999, 'late', ?, 'pending', 0, 0, 0)",
                     (json.dumps({"account_id": "9", "conversation_id": "-3000", "conversation": {
                         "chat_id": "-3000", "chat_type": "group", "title": "Late"}}),))
    CustodyStore(path)
    assert "Late (-3000)" not in listed(store) and len(listed(store)) == 260
    assert backfill_marker(path)["after"] == 275


def test_an_interrupted_backfill_resumes_and_never_undoes_newer_evidence(tmp_path, monkeypatch):
    path = tmp_path / "custody.sqlite3"
    legacy_custody(path, groups=120)
    before = custody_rows(path)
    monkeypatch.setattr(discovery, "_BACKFILL_BATCH", 50)
    readable, calls = discovery._captured_group, []

    def stopped_in_second_batch(payload):
        calls.append(payload)
        if len(calls) == 60:
            raise RuntimeError("process stopped")
        return readable(payload)

    monkeypatch.setattr(discovery, "_captured_group", stopped_in_second_batch)
    with pytest.raises(RuntimeError):
        CustodyStore(path)
    assert backfill_marker(path) == {"after": 50, "done": False, "unreadable": 0}
    monkeypatch.setattr(discovery, "_captured_group", readable)

    # Newer evidence for groups the backfill has not reached yet: a removal, a rename, a migration.
    live = CustodyStore.__new__(CustodyStore)  # records updates without starting a backfill of its own
    live.path = path
    commit(live, member(1000, chat(-1060, "Group 060"), "left"),
           said(1001, chat(-1070, "Group 070 renamed"), "", new_chat_title="Group 070 renamed"),
           said(1002, chat(-1080, "Group 080", "group"), "", migrate_to_chat_id=-5080))
    CustodyStore(path)  # the backfill resumes after its first batch
    offered = listed(live)
    assert "Group 060 (-1060)" not in offered and "Group 080 (-1080)" not in offered
    assert "Group 070 renamed (-1070)" in offered and "Group 070 (-1070)" not in offered
    assert len(offered) == 118 and "Group 000 (-1000)" in offered and "Group 119 (-1119)" in offered
    assert backfill_marker(path) == {"after": 135, "done": True, "unreadable": len(_INVALID)}
    # Only the offset moved, for the live updates.
    after = custody_rows(path)
    assert {key: rows for key, rows in after.items() if key != "metadata"} == {
        key: rows for key, rows in before.items() if key != "metadata"}
    assert after["metadata"] == [("telegram_offset", "1003")]
