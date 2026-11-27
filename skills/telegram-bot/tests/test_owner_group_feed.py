"""Focused offline tests for wire-v2 owner-bot group feed A alongside unchanged standalone B."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.parse
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def _load_plugin() -> Any:
    name = "telegram_bot_owner_feed_test_pkg"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        SKILL_ROOT / "plugin.py",
        submodule_search_locations=[str(SKILL_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
from telegram_bot_owner_feed_test_pkg.telegram_bot.api import (  # noqa: E402
    TelegramApiError,
    TelegramClient,
    TelegramRequestNotSent,
)
from telegram_bot_owner_feed_test_pkg.telegram_bot.custody import CALL_STARTED, CustodyStore  # noqa: E402
from telegram_bot_owner_feed_test_pkg.telegram_bot.events import parse_telegram_update  # noqa: E402
from telegram_bot_owner_feed_test_pkg.telegram_bot.host import (  # noqa: E402
    HOLD_PLACE_NOT_CONNECTED,
    PresenceHostClient,
    PresenceHostHTTPError,
)
from telegram_bot_owner_feed_test_pkg.telegram_bot.owner_feed import (  # noqa: E402
    HOLD_ACCOUNT_MISMATCH,
    HOLD_AUTH_UNAVAILABLE,
    HOLD_RELAY_BUSY,
    OwnerFeedClient,
    RELAY_API_SERVER,
    RelayRefusalError,
    RelayTelegramTransport,
    SOURCE_OWNER_BOT,
    owner_state_dir,
    owner_store_exists,
    owner_store_path,
)
from telegram_bot_owner_feed_test_pkg.telegram_bot.runtime import (  # noqa: E402
    BOT_CONFLICT_WITH_OWNER,
    TelegramTransportRuntime,
)


CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"


def _binding(
    account_id: str = "1001",
    conversation_id: str = "-100500",
    *,
    thread_id: str = "",
    place_id: str = "place-a-500",
    connected_revision: int = 1,
) -> Dict[str, Any]:
    return {
        "version": 1,
        "connector": "telegram-bot",
        "provider": "telegram",
        "account_id": account_id,
        "conversation_id": conversation_id,
        "thread_id": thread_id,
        "place_id": place_id,
        "connected_revision": connected_revision,
    }


def _v2_identity(
    account_id: str = "1001",
    *,
    label: str = "@owner_bot",
    enabled: bool = True,
    reason: str = "",
    owner_bound: bool = True,
    invite_url: str = "https://t.me/owner_bot?startgroup=true",
    same_as_owner_bridge: Optional[bool] = False,
    include_identity: bool = True,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "v": 1,
        "account_id": account_id,
        "owner_bound": owner_bound,
        "invite_url": invite_url,
        "enabled": enabled,
        "reason": reason,
        "same_as_owner_bridge": same_as_owner_bridge,
    }
    if include_identity and account_id:
        payload["identity"] = {"sends_as": {"id": account_id, "label": label, "kind": "bot"}}
    return payload


class _FakeApi:
    def __init__(self, state_dir: pathlib.Path, *, bot_token: str = "", skill_token: str = "skill-tok"):
        self.state_dir = state_dir
        self.bot_token = bot_token
        self.skill_token = skill_token
        self.tasks: List[Tuple[str, Any, Dict[str, Any]]] = []
        self.tools: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
        self.routes: Dict[str, Any] = {}
        self.logs: List[Tuple[str, str]] = []
        self.unload_cb = None

    def get_state_dir(self) -> str:
        return str(self.state_dir)

    def get_settings(self, keys: List[str]) -> Dict[str, str]:
        return {"TELEGRAM_PUBLIC_BOT_TOKEN": self.bot_token} if "TELEGRAM_PUBLIC_BOT_TOKEN" in keys else {}

    def get_skill_token(self) -> Any:
        return SimpleNamespace(use_in_request=lambda: self.skill_token)

    def register_supervised_task(self, name: str, fn: Any, **options: Any) -> None:
        self.tasks.append((name, fn, options))

    def register_tool(self, name: str, fn: Any, *, description: str = "", schema: Optional[Dict[str, Any]] = None) -> None:
        self.tools[name] = (fn, schema or {})

    def register_route(self, path: str, fn: Any, *, methods: Tuple[str, ...] = ("GET",)) -> None:
        self.routes[path] = fn

    def register_settings_section(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def register_ui_tab(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def on_unload(self, cb: Any) -> None:
        self.unload_cb = cb

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


class _FakeRequest:
    def __init__(self, query: Optional[Dict[str, str]] = None, method: str = "GET"):
        self.query_params = dict(query or {})
        self.method = method


class _ScriptedHostTransport:
    """In-memory Host Service + wire-v2 /telegram/feed/* + /presence/place bind/check simulator."""

    def __init__(
        self,
        *,
        identity_caps: Dict[str, Any],
        feed_identity: Any = None,
        feed_updates: Optional[List[Any]] = None,
        poller_handler: Any = None,
        call_handler: Any = None,
        upload_handler: Any = None,
        turn_handler: Any = None,
        work_handler: Any = None,
        owner_places: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
        place_states: Optional[Dict[Tuple[str, str], str]] = None,
    ):
        self.identity_caps = dict(identity_caps)
        self.feed_identity = feed_identity
        self.feed_updates = list(feed_updates or [])
        self.poller_handler = poller_handler
        self.call_handler = call_handler
        self.upload_handler = upload_handler
        self.turn_handler = turn_handler
        self.work_handler = work_handler
        # Map (account_id, conversation_id) -> {"place_id": str, "connected_revision": int, "state": str, "label": str}
        self.owner_places = dict(owner_places or {})
        self.place_states = dict(place_states or {})
        self.requests: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
        self.place_queries: List[Dict[str, str]] = []
        self.turns: List[Dict[str, Any]] = []
        self.reports: List[Dict[str, Any]] = []
        self.feed_calls: List[Dict[str, Any]] = []
        self.feed_uploads: List[Dict[str, Any]] = []
        self.updates_queries: List[Dict[str, str]] = []
        self.identity_queries: List[Dict[str, str]] = []
        self.poller_claims: List[Tuple[Dict[str, str], Optional[Dict[str, Any]]]] = []
        self._feed_polled: Optional[asyncio.Event] = None

    @property
    def feed_polled(self) -> asyncio.Event:
        if self._feed_polled is None:
            self._feed_polled = asyncio.Event()
        return self._feed_polled

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout_sec: float,
    ) -> Dict[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        self.requests.append((method, parsed.path, payload))

        if parsed.path == "/identity":
            return dict(self.identity_caps)

        if parsed.path == "/telegram/feed/identity":
            self.identity_queries.append(query)
            if callable(self.feed_identity):
                result = self.feed_identity(query)
                if isinstance(result, Exception):
                    raise result
                return result
            return dict(self.feed_identity or _v2_identity("", enabled=False, reason="feed_off", include_identity=False))

        if parsed.path == "/telegram/feed/poller":
            assert method == "POST"
            self.poller_claims.append((dict(headers), dict(payload) if isinstance(payload, dict) else payload))
            if callable(self.poller_handler):
                result = self.poller_handler(payload, headers)
                if isinstance(result, Exception):
                    raise result
                return result
            return {"ok": True, "owned": True}

        if parsed.path == "/telegram/feed/updates":
            self.updates_queries.append(query)
            self.feed_polled.set()
            if self.feed_updates:
                batch = self.feed_updates.pop(0)
                if isinstance(batch, Exception):
                    raise batch
                return {"ok": True, "result": batch}
            await asyncio.sleep(0.05)
            return {"ok": True, "result": []}

        if parsed.path == "/telegram/feed/call":
            assert payload is not None
            self.feed_calls.append(payload)
            if callable(self.call_handler):
                result = self.call_handler(payload)
                if isinstance(result, Exception):
                    raise result
                return result
            return {
                "ok": True,
                "result": {
                    "message_id": 500 + len(self.feed_calls),
                    "chat": {"id": int(payload["params"]["chat_id"]), "type": "supergroup"},
                },
            }

        if parsed.path == "/telegram/feed/upload":
            assert payload is not None
            self.feed_uploads.append(payload)
            if callable(self.upload_handler):
                result = self.upload_handler(payload)
                if isinstance(result, Exception):
                    raise result
                return result
            method_name = payload["method"]
            chat_id = int(payload["params"]["chat_id"])
            if method_name == "sendMediaGroup":
                items = payload["params"]["media"]
                return {
                    "ok": True,
                    "result": [
                        {"message_id": 700 + i, "chat": {"id": chat_id, "type": "supergroup"}}
                        for i in range(len(items))
                    ],
                }
            return {
                "ok": True,
                "result": {"message_id": 600 + len(self.feed_uploads), "chat": {"id": chat_id, "type": "supergroup"}},
            }

        if parsed.path == "/presence/place":
            self.place_queries.append(query)
            op = query.get("operation", "")
            acct = query.get("account_id", "")
            conv = query.get("conversation_id", "")
            thread = query.get("thread_id", "")
            if op in ("bind", "check"):
                assert query.get("contribution_id") == "owner_bot"
                assert query.get("require_place") == "true"
                assert query.get("provider") == "telegram"
                record = self.owner_places.get((acct, conv))
                if op == "bind":
                    if record is None:
                        raise PresenceHostHTTPError(
                            404,
                            "place not connected",
                            {"ok": False, "allowed": False, "binding": None, "state": "unknown",
                             "code": "place_not_connected", "error": "Connect this group in Places first."},
                        )
                    if record.get("state") == "auth_blocked":
                        raise PresenceHostHTTPError(
                            403,
                            "auth blocked",
                            {"ok": False, "code": "presence_auth_blocked", "error": "Check skill access."},
                        )
                    bnd = _binding(
                        acct,
                        conv,
                        thread_id=thread,
                        place_id=record["place_id"],
                        connected_revision=record["connected_revision"],
                    )
                    state = record.get("state", "active")
                    return {
                        "ok": True,
                        "allowed": state == "active",
                        "binding": bnd,
                        "state": state,
                        "code": f"place_{state}",
                        "label": record.get("label", f"Telegram {acct} {conv}"),
                        "place_id": record["place_id"],
                    }
                # operation == "check"
                retained = json.loads(query["binding"])
                assert retained["connector"] == "telegram-bot"
                assert retained["provider"] == "telegram"
                assert retained["account_id"] == acct
                assert retained["conversation_id"] == conv
                assert retained["thread_id"] == thread
                if record is None or record["place_id"] != retained["place_id"]:
                    return {
                        "ok": True,
                        "allowed": False,
                        "binding": retained,
                        "state": "disconnected",
                        "code": "place_disconnected",
                        "label": record.get("label", "") if record else "",
                        "place_id": retained["place_id"],
                    }
                if record["connected_revision"] != retained["connected_revision"]:
                    return {
                        "ok": True,
                        "allowed": False,
                        "binding": retained,
                        "state": "disconnected",
                        "code": "place_disconnected",
                        "label": record.get("label", ""),
                        "place_id": record["place_id"],
                    }
                state = record.get("state", "active")
                return {
                    "ok": True,
                    "allowed": state == "active",
                    "binding": retained,
                    "state": state,
                    "code": f"place_{state}",
                    "label": record.get("label", f"Telegram {acct} {conv}"),
                    "place_id": record["place_id"],
                }

            # Plain legacy B lookup (never receives operation or contribution_id).
            assert "operation" not in query and "contribution_id" not in query
            key = (acct, conv)
            state = self.place_states.get(key, "active")
            if state == "not_connected":
                raise PresenceHostHTTPError(
                    404,
                    "place not connected",
                    {"code": "place_not_connected", "error": "Connect this chat in Places first."},
                )
            return {"state": state, "label": f"Telegram {key[0]} {key[1]}"}

        if parsed.path == "/presence/turn":
            assert payload is not None
            self.turns.append(payload)
            if callable(self.turn_handler):
                return self.turn_handler(payload)
            return {
                "status": "completed",
                "outcome": "silent",
                "text": "",
                "turn_ref": f"turn-{len(self.turns)}",
                "delivery_reporting_version": 1,
            }

        if parsed.path.startswith("/presence/work/"):
            work_ref = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            if callable(self.work_handler):
                return self.work_handler(work_ref, query)
            return {"status": "completed", "outcome": "silent", "text": "", "work_ref": work_ref}

        if parsed.path == "/presence/delivery":
            assert payload is not None
            self.reports.append(payload)
            return {"ok": True, "recorded": True}

        raise AssertionError(f"unexpected Host request: {method} {url}")


class _FakeTelegramTransport:
    def __init__(
        self,
        *,
        bot_id: int = 2002,
        username: str = "standalone_bot",
        updates: Optional[List[List[Dict[str, Any]]]] = None,
    ):
        self.bot_id = bot_id
        self.username = username
        self.updates = list(updates or [])
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.polled = asyncio.Event()

    async def post_json(self, url: str, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(payload)))
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        if method == "getMe":
            return {"ok": True, "result": {"id": self.bot_id, "is_bot": True, "username": self.username}}
        if method == "getUpdates":
            self.polled.set()
            if self.updates:
                return {"ok": True, "result": self.updates.pop(0)}
            await asyncio.sleep(0.05)
            return {"ok": True, "result": []}
        if method == "sendMessage":
            return {
                "ok": True,
                "result": {"message_id": 900 + len(self.calls), "chat": {"id": int(payload["chat_id"]), "type": "supergroup"}},
            }
        return {"ok": True, "result": True}

    async def post_multipart(
        self,
        url: str,
        fields: Dict[str, str],
        file_field: str,
        file_path: pathlib.Path,
        timeout_sec: float,
        progress: Any = None,
    ) -> Dict[str, Any]:
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(fields)))
        size = pathlib.Path(file_path).stat().st_size
        if progress is not None:
            progress.begin(size)
            progress.wrote(size)
            progress.sent()
            progress.ended("ok", 0)
        return {
            "ok": True,
            "result": {"message_id": 950 + len(self.calls), "chat": {"id": int(fields["chat_id"]), "type": "supergroup"}},
        }

    async def post_multipart_files(
        self,
        url: str,
        fields: Dict[str, str],
        files: Any,
        timeout_sec: float,
        progress: Any = None,
    ) -> Dict[str, Any]:
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(fields)))
        return {"ok": True, "result": []}

    async def get_bytes(self, url: str, timeout_sec: float, max_bytes: int) -> bytes:
        return b"standalone-bytes"


def _json_body(response: Any) -> Dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


async def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


def test_old_core_without_capability_never_touches_feed_or_creates_owner_store(tmp_path):
    async def run_case():
        api = _FakeApi(tmp_path / "state", bot_token="2002:standalone-secret")
        plugin.register(api)
        host = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "presence_identity": 1,
                "presence_reaction_context": 1,
                "presence_explicit_completion": 1,
            },
        )
        tg_b = _FakeTelegramTransport(bot_id=2002, username="b_bot")
        plugin._RUNTIME.submitter.transport = host
        plugin._OWNER_RUNTIME.submitter.transport = host
        plugin._RUNTIME.client_factory = lambda token: TelegramClient(token, transport=tg_b)

        # Before start: unobserved owner_bot_status returns enabled: null, connect: null, no account/identity.
        pre_status = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
        assert pre_status == {"v": 1, "enabled": None, "reason": "not_started", "connect": None}

        [(_name, supervised, _opts)] = api.tasks
        task = asyncio.create_task(supervised())
        try:
            await asyncio.wait_for(tg_b.polled.wait(), timeout=3.0)
            await _wait_until(lambda: plugin._OWNER_RUNTIME._runtime_state == "unsupported")

            b_status = _json_body(await api.routes["status"](_FakeRequest()))
            a_status = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert b_status["bot_label"] == "@b_bot"
            assert b_status["runtime_state"] == "polling"
            assert "source" not in b_status

            assert a_status == {"v": 1, "enabled": None, "reason": "unsupported", "connect": None}
            assert not owner_state_dir(api.state_dir).exists()
            assert not any(path.startswith("/telegram/feed/") for _, path, _ in host.requests)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            api.unload_cb()

    asyncio.run(run_case())


def test_standalone_tool_reply_without_an_account_keeps_its_conversations_bot(tmp_path, monkeypatch):
    api = _FakeApi(tmp_path / "state", bot_token="2002:synthetic")
    store = CustodyStore.shared(api.state_dir / "custody.sqlite3")
    update = {"update_id": 10, "message": {
        "message_id": 15, "message_thread_id": 9, "is_topic_message": True,
        "date": 1730000000, "text": "hello", "from": {"id": 55, "first_name": "Bob"},
        "chat": {"id": -100500, "type": "supergroup", "is_forum": True},
    }}
    event = parse_telegram_update(update, bot_account_id="2002", places=True)
    store.commit_update(10, event)
    ctx = SimpleNamespace(task_id="turn-1", task_metadata={"presence": {"event": event.to_dict()}})
    send = plugin._make_telegram_send(api, standalone_account=lambda: "2002", standalone_configured=lambda: True)
    receipt = send(ctx, chat_id="-100500", topic_id="9", reply_to_message_id="15", text="Hi Bob", request_id="reply")
    assert receipt == {"ok": True, "state": "queued", "request_id": "reply", "chat_id": "-100500", "kind": "message"}
    payload = store.outbox_payload("telegram-send:reply")
    assert (payload["account_id"], payload["topic_id"], payload["reply_to_message_id"]) == ("2002", "9", "15")
    assert send(ctx, account_id="2002", chat_id="-100500", topic_id="9", text="Hi Bob",
                request_id="named")["account_id"] == "2002"
    assert "account_id" not in send(ctx, chat_id="-100600", text="Hi everyone", request_id="elsewhere")
    assert "account_id" not in store.outbox_payload("telegram-send:elsewhere")
    receipt_tool = plugin.make_receipt_tool(api)
    assert receipt_tool(request_id="elsewhere") == store.delivery_receipt("telegram-send:elsewhere")
    assert receipt_tool(request_id="elsewhere", account_id="2002") == {"state": "not_found"}
    owner_store = CustodyStore.shared(owner_store_path(api.state_dir))
    owner_event = parse_telegram_update(update, bot_account_id="1001", places=True, group_only=True)
    owner_binding = _binding(thread_id="9")
    owner_store.commit_update(10, owner_event, place_binding=owner_binding)
    owner_store.enqueue_outbox("telegram-send:reply", {"kind": "message", "chat_id": "-100500", "topic_id": "9",
                                                       "account_id": "1001", "_place_binding": owner_binding})
    owner_ctx = SimpleNamespace(task_id="owner-turn", task_metadata={"presence": {"event": owner_event.to_dict()}})
    # The chosen bot for a separate conversation is independent of the Telegram turn that started this work.
    separate = send(owner_ctx, chat_id="-100600", account_id="2002", text="Meeting notes",
                    request_id="separate")
    assert (separate["state"], separate["account_id"]) == ("queued", "2002")
    assert store.outbox_payload("telegram-send:separate")["chat_id"] == "-100600"
    # A document reply to the original message keeps that account and the retained topic connection.
    document = tmp_path / "notes.txt"
    document.write_text("Meeting notes", encoding="utf-8")
    paired_send = plugin._make_telegram_send(api, standalone_account=lambda: "2002", owner_account=lambda: "1001")
    sent_document = paired_send(owner_ctx, chat_id="-100500", account_id="1001", topic_id="9",
                                reply_to_message_id="15", kind="document", file_path=str(document),
                                request_id="document")
    assert (sent_document["state"], sent_document["account_id"]) == ("queued", "1001")
    retained = owner_store.outbox_payload("telegram-send:document")
    assert (retained["account_id"], retained["reply_to_message_id"], retained["_place_binding"]) == (
        "1001", "15", owner_binding,
    )
    assert receipt_tool(request_id="reply") == store.delivery_receipt("telegram-send:reply")
    assert receipt_tool(owner_ctx, request_id="reply") == owner_store.delivery_receipt("telegram-send:reply")
    assert receipt_tool(owner_ctx, request_id="reply", account_id="2002") == store.delivery_receipt("telegram-send:reply")
    assert receipt_tool(owner_ctx, request_id="elsewhere") == {"state": "not_found"}

    def unavailable(_source_id):
        raise OSError("cannot read the delivery history now")

    with monkeypatch.context() as m:
        m.setattr(owner_store, "inbox_place_binding", unavailable)
        assert receipt_tool(owner_ctx, request_id="reply")["code"] == "receipt_source_unavailable"
        assert receipt_tool(request_id="reply") == store.delivery_receipt("telegram-send:reply")
    owner_store.close()
    store.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_standalone_actions_pin_the_event_bot_without_extending_a_legacy_response(tmp_path, explicit):
    api = _FakeApi(tmp_path / "state", bot_token="2002:synthetic")
    store = CustodyStore.shared(api.state_dir / "custody.sqlite3")
    update = {"update_id": 20, "message": {"message_id": 21, "date": 1730000000, "text": "hello",
              "from": {"id": 55, "first_name": "Bob"}, "chat": {"id": -100500, "type": "supergroup"}}}
    event = parse_telegram_update(update, bot_account_id="2002", places=True)
    store.commit_update(20, event)
    store.claim_inbox()
    ctx = SimpleNamespace(task_id="turn-20", task_metadata={"presence": {"event": event.to_dict()}})
    chat = "-100500"
    account = {"account_id": "2002"} if explicit else {}
    moderation = plugin.make_moderation_tool(api, standalone_account=lambda: "2002")
    operation = plugin.make_operation_tool(api, confirmed_account=lambda: "2002")
    reaction = plugin.make_react_tool(operation)
    typing = plugin.make_typing_tool(api, standalone_account=lambda: "2002")

    assert moderation(ctx, action="delete_message", chat_id=chat, message_id=21, request_id="delete", **account) == {
        "ok": True, "state": "queued", "request_id": "delete", "action": "delete_message", **account,
    }
    common = {"ok": True, "state": "queued", "method": "setMessageReaction", "chat_id": chat, "message_id": 21}
    assert operation(ctx, method="setMessageReaction", chat_id=chat, message_id=21, request_id="set",
                     reaction=[{"type": "emoji", "emoji": "👍"}], **account) == {**common, "request_id": "set", **account}
    assert reaction(ctx, action="clear", chat_id=chat, message_id=21, request_id="clear", **account) == {
        **common, "request_id": "clear", **account,
    }
    requested = typing(ctx, chat_id=chat, **account)
    assert requested == {"ok": True, "typing": "requested", "chat_id": chat, "note": requested["note"], **account}
    assert store.typing_requests()[0]["source_event_id"] == event.source_event_id
    for key in ("moderate:delete", "operation:set", "operation:clear"):
        assert store.outbox_payload(f"telegram-{key}")["account_id"] == "2002"
    store.close()


@pytest.mark.parametrize("owner_event", [False, True])
def test_unreadable_turn_source_does_not_route_the_turns_action_as_another_bot(tmp_path, monkeypatch, owner_event):
    api = _FakeApi(tmp_path / "state", bot_token="2002:synthetic")
    store = CustodyStore.shared(owner_store_path(api.state_dir) if owner_event else api.state_dir / "custody.sqlite3")
    account = "1001" if owner_event else "2002"
    chat = "-100500"
    update = {"update_id": 30, "message": {"message_id": 31, "date": 1730000000, "text": "hello",
              "from": {"id": 55, "first_name": "Bob"}, "chat": {"id": int(chat), "type": "supergroup"}}}
    event = parse_telegram_update(update, bot_account_id=account, places=True, group_only=owner_event)
    store.commit_update(30, event, **({"place_binding": _binding()} if owner_event else {}))
    ctx = SimpleNamespace(task_id="turn-30", task_metadata={"presence": {"event": event.to_dict()}})

    def unavailable(_event_id):
        raise OSError("temporarily unreadable")

    monkeypatch.setattr(store, "inbox_event", unavailable)
    config = {"standalone_account": lambda: "2002", "owner_account": lambda: "1001"}
    send = plugin._make_telegram_send(api, **config)
    typing = plugin.make_typing_tool(api, **config)
    moderate = plugin.make_moderation_tool(api, **config)
    react = plugin.make_react_tool(plugin.make_operation_tool(api, confirmed_account=config["standalone_account"],
                                                             owner_account=config["owner_account"]))
    assert send(ctx, chat_id=chat, text="Here", request_id="same")["code"] == "turn_source_unavailable"
    assert typing(ctx, chat_id=chat, account_id=account)["code"] == "turn_source_unavailable"
    assert moderate(ctx, action="delete_message", chat_id=chat, message_id=31,
                    request_id="delete")["code"] == "turn_source_unavailable"
    assert react(ctx, action="clear", chat_id=chat, message_id=31,
                 request_id="react", account_id=account)["code"] == "turn_source_unavailable"
    b_store = CustodyStore.shared(api.state_dir / "custody.sqlite3")
    for row in (store, b_store):
        assert row.typing_requests() == []
        assert all(row.delivery_receipt(f"telegram-{key}") == {"state": "not_found"}
                   for key in ("send:same", "moderate:delete", "operation:react"))
    # An A turn already required a named bot; a standalone turn still uses legacy B for unrelated destinations.
    other_account = {"account_id": "2002"} if owner_event else {}
    assert send(ctx, chat_id="-100600", text="Elsewhere", request_id="other", **other_account)["state"] == "queued"
    assert send(ctx, chat_id=chat, topic_id="5", text="Different topic", request_id="topic",
                **other_account)["state"] == "queued"
    assert b_store.outbox_payload("telegram-send:other").get("account_id") == other_account.get("account_id")
    store.close()
    b_store.close()


def test_explicit_receipt_does_not_guess_between_two_retained_roles(tmp_path, monkeypatch):
    api = _FakeApi(tmp_path / "state", bot_token="2002:synthetic")
    standalone = CustodyStore.shared(api.state_dir / "custody.sqlite3")
    owner = CustodyStore.shared(owner_store_path(api.state_dir))
    receipt = plugin.make_receipt_tool(api)
    row = {"kind": "message", "chat_id": "-100500", "account_id": "2002"}
    delivery = "telegram-send:recorded"
    standalone.enqueue_outbox(delivery, row)
    assert receipt(request_id="recorded", account_id="2002") == standalone.delivery_receipt(delivery)
    owner.enqueue_outbox(delivery, {**row, "_place_binding": _binding(account_id="2002")})
    assert receipt(request_id="recorded", account_id="2002")["code"] == "receipt_source_ambiguous"
    assert receipt(request_id="recorded", account_id="3003") == {"state": "not_found"}
    assert receipt(request_id="recorded") == standalone.delivery_receipt(delivery)

    def unavailable(_delivery_id):
        raise OSError("temporarily unreadable")

    monkeypatch.setattr(owner, "delivery_receipt", unavailable)
    assert receipt(request_id="recorded", account_id="2002")["code"] == "receipt_source_unavailable"
    assert receipt(request_id="recorded") == standalone.delivery_receipt(delivery)
    owner.close()
    standalone.close()


def test_owner_bot_status_ui_contract_all_states_and_never_returns_b(tmp_path):
    async def run_case():
        api = _FakeApi(tmp_path / "state", bot_token="2002:standalone-secret")
        plugin.register(api)
        feed_state: Dict[str, Any] = {
            "resp": _v2_identity("1001", enabled=False, reason="feed_off", same_as_owner_bridge=False, include_identity=False),
        }
        host = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "presence_identity": 1,
                "telegram_group_feed": 1,
            },
            feed_identity=lambda _q: (
                feed_state["resp"] if isinstance(feed_state["resp"], Exception) else dict(feed_state["resp"])
            ),
        )
        tg_b = _FakeTelegramTransport(bot_id=2002, username="b_bot")
        plugin._RUNTIME.submitter.transport = host
        plugin._OWNER_RUNTIME.submitter.transport = host
        plugin._RUNTIME.client_factory = lambda token: TelegramClient(token, transport=tg_b)

        [(_name, supervised, _opts)] = api.tasks
        task = asyncio.create_task(supervised())
        try:
            await asyncio.wait_for(tg_b.polled.wait(), timeout=3.0)
            await _wait_until(lambda: plugin._OWNER_RUNTIME._runtime_state == "feed_off")

            # 1. Feed off (disabled): enabled=False, reason="feed_off", connect=None, NO account_id/identity/invite_url.
            off_status = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert off_status == {"v": 1, "enabled": False, "reason": "feed_off", "connect": None}
            off_match = _json_body(await api.routes["owner_bot_status"](_FakeRequest({"account_id": "1001"})))
            assert off_match == {"v": 1, "enabled": False, "reason": "feed_off", "connect": None}
            # Querying B's account_id returns mismatch (enabled=None, connect=None), never B.
            off_b = _json_body(await api.routes["owner_bot_status"](_FakeRequest({"account_id": "2002"})))
            assert off_b == {"v": 1, "enabled": None, "reason": "account_mismatch", "connect": None}
            assert not host.updates_queries
            assert not owner_store_exists(api.state_dir)

            # 2. Read unavailable states -> enabled=None (distinct from disabled enabled=False!), connect=None.
            feed_state["resp"] = PresenceHostHTTPError(
                403,
                "auth unavailable",
                {"ok": False, "error_code": 403, "description": "auth check unavailable",
                 "relay_refusal": "auth_unavailable", "parameters": {"retry_after": 5}},
            )
            auth_info = await plugin._OWNER_RUNTIME.owner_feed.identity()
            assert auth_info.enabled is False and auth_info.reason == "auth_unavailable" and auth_info.same_as_owner_bridge is None
            for unavailable_reason in ("identity_unconfirmed", "auth_unavailable", "feed_unavailable", "bridge_not_ready"):
                plugin._OWNER_RUNTIME._clear_owner_identity(
                    state="waiting_for_owner_bot",
                    reason=unavailable_reason,
                    enabled=False,
                    observed=True,
                    forget_recorded=False,
                )
                unavail = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
                assert unavail == {"v": 1, "enabled": None, "reason": unavailable_reason, "connect": None}
            plugin._OWNER_RUNTIME._clear_owner_identity(
                state="provider_error",
                reason="host_unavailable",
                enabled=False,
                observed=False,
                forget_recorded=False,
            )
            host_unavail = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert host_unavail == {"v": 1, "enabled": None, "reason": "host_unavailable", "connect": None}

            # 3. Verified current A ready with 0 groups -> connect=[] (confirmed empty) + exact account_id/identity/invite_url.
            feed_state["resp"] = _v2_identity("1001", label="@owner_bot", enabled=True, reason="", same_as_owner_bridge=False)
            info = await plugin._OWNER_RUNTIME.owner_feed.identity()
            plugin._OWNER_RUNTIME._activate_owner_client(info)
            older_identity = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert older_identity["enabled"] is True and older_identity["connect"] is None
            feed_state["resp"]["group_ids"] = []
            ready_empty = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert ready_empty == {
                "v": 1,
                "enabled": True,
                "reason": "",
                "account_id": "1001",
                "identity": {"sends_as": {"id": "1001", "label": "@owner_bot", "kind": "bot"}},
                "connect": [],
                "invite_url": "https://t.me/owner_bot?startgroup=true",
            }
            ready_match = _json_body(await api.routes["owner_bot_status"](_FakeRequest({"account_id": "1001"})))
            assert ready_match == ready_empty
            ready_mismatch_b = _json_body(await api.routes["owner_bot_status"](_FakeRequest({"account_id": "2002"})))
            assert ready_mismatch_b == {"v": 1, "enabled": None, "reason": "account_mismatch", "connect": None}
            assert "account_id" not in ready_mismatch_b and "identity" not in ready_mismatch_b

            # The group appears on a native sighting, even before its title reaches the connector.
            feed_state["resp"]["group_ids"] = ["-100500"]
            recent = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert recent["connect"] == [{"account_id": "1001", "conversation_id": "-100500", "label": "Group (-100500)"}]
            owner = plugin._OWNER_RUNTIME
            owner._commit_feed_item({"feed_id": 1, "sighting": {"chat": {"id": -100500, "type": "supergroup",
                                                                       "title": "Fresh Room"}, "membership": "present"}},
                                    account="1001", username="owner_bot", download_limit=1)
            labeled = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert labeled["connect"][0]["label"] == "Fresh Room (-100500)"
            # The bridge goes Off then On between worker polls; the old local sighting remains only as a title.
            feed_state["resp"] = _v2_identity("1001", enabled=False, reason="feed_off", include_identity=False)
            assert _json_body(await api.routes["owner_bot_status"](_FakeRequest())) == off_status
            feed_state["resp"] = {**_v2_identity("1001"), "group_ids": []}
            assert _json_body(await api.routes["owner_bot_status"](_FakeRequest())) == ready_empty

            # 4. Unchanged B status route NEVER switches to A even when ?account_id=1001 is passed.
            b_plain = _json_body(await api.routes["status"](_FakeRequest()))
            b_with_query = _json_body(await api.routes["status"](_FakeRequest({"account_id": "1001"})))
            assert b_plain["bot_label"] == "@b_bot"
            assert b_with_query["bot_label"] == "@b_bot"
            prior_native = len(host.identity_queries)
            await api.routes["status"](_FakeRequest())
            assert any(q == {"groups": "1"} for q in host.identity_queries)
            assert not any("groups" in q for q in host.identity_queries[prior_native:])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            api.unload_cb()

    asyncio.run(run_case())


def test_owner_feed_v2_sightings_raw_types_media_retry_and_outbound_end_to_end(tmp_path):
    async def run_case():
        api = _FakeApi(tmp_path / "state", bot_token="")
        plugin.register(api)

        photo_bytes = b"\xff\xd8\xff\xe0synthetic-jpeg-inbound-bytes"
        downloaded_urls: List[str] = []
        dl_state = {"fail_first": True}

        def call_handler(body: Dict[str, Any]) -> Dict[str, Any]:
            # Wire v2: every relay call carries exact sideband keys.
            assert set(body) >= {"account_id", "place_id", "connected_revision", "thread_id", "method", "params"}
            assert body["account_id"] == "1001"
            assert body["place_id"] == "p-team"
            assert body["connected_revision"] == 2
            method = body["method"]
            params = body["params"]
            if method == "getFile":
                assert params == {"file_id": "photo-file-1"}
                assert body["thread_id"] == "77"
                return {
                    "ok": True,
                    "result": {"file_id": "photo-file-1", "file_size": len(photo_bytes), "file_path": "photos/p1.jpg"},
                }
            if method == "sendMessage":
                return {
                    "ok": True,
                    "result": {"message_id": 401, "chat": {"id": int(params["chat_id"]), "type": "supergroup"}, "text": params["text"]},
                }
            if method == "editMessageText":
                assert "message_thread_id" not in params
                assert body["thread_id"] == "77"
                return {
                    "ok": True,
                    "result": {"message_id": int(params["message_id"]), "chat": {"id": int(params["chat_id"]), "type": "supergroup"}, "text": params["text"]},
                }
            if method in ("setMessageReaction", "sendChatAction", "deleteMessage"):
                return {"ok": True, "result": True}
            raise AssertionError(f"unexpected call method: {method}")

        def bytes_transport(url: str, headers: Dict[str, str], timeout_sec: float, max_bytes: int) -> bytes:
            assert headers.get("X-Skill-Token") == "skill-tok"
            downloaded_urls.append(url)
            if dl_state["fail_first"]:
                dl_state["fail_first"] = False
                raise TelegramApiError("download", "transient relay hiccup", error_code=0)
            return photo_bytes

        def turn_handler(body: Dict[str, Any]) -> Dict[str, Any]:
            # Wire v2: every A turn sends binding_id and connected_revision.
            assert body["binding_id"] == "p-team"
            assert body["connected_revision"] == 2
            msg = body["event"]["message"]
            if msg.get("event_kind") == "message" and msg.get("message_id") == 88:
                return {
                    "status": "completed",
                    "outcome": "deferred",
                    "text": "immediate ack",
                    "turn_ref": "turn-88",
                    "work_ref": "work-88",
                    "delivery_reporting_version": 1,
                }
            return {
                "status": "completed",
                "outcome": "silent",
                "text": "",
                "turn_ref": f"turn-{msg.get('message_id')}",
                "delivery_reporting_version": 1,
            }

        def work_handler(work_ref: str, query: Dict[str, str]) -> Dict[str, Any]:
            assert query.get("binding_id") == "p-team"
            return {
                "status": "completed",
                "outcome": "message",
                "text": "deferred final answer",
                "work_ref": work_ref,
                "delivery_reporting_version": 1,
            }

        place_tag = {"place_id": "p-team", "connected_revision": 2, "thread_id": ""}
        batch_1 = [
            # Ordinary group sightings: old group migrated to supergroup (retires old -100400).
            {"feed_id": 10, "sighting": {"chat": {"id": -100400, "type": "group", "title": "Old Room"}, "membership": "present"}},
            {
                "feed_id": 11,
                "sighting": {
                    "chat": {"id": -100500, "type": "supergroup", "title": "Team Room"},
                    "membership": "present",
                    "migrate_from_chat_id": -100400,
                },
            },
            # Ordinary forum-topic photo message (retries attachment download AFTER spool ACK).
            {
                "feed_id": 12,
                "account_id": "1001",
                "place": place_tag,
                "update": {
                    "update_id": 900000123,
                    "message": {
                        "message_id": 88,
                        "message_thread_id": 77,
                        "is_topic_message": True,
                        "date": 1730000000,
                        "from": {"id": 555, "is_bot": False, "first_name": "Bob"},
                        "chat": {"id": -100500, "type": "supergroup", "title": "Team Room", "is_forum": True},
                        "caption": "look at this",
                        "photo": [{"file_id": "photo-file-1", "file_unique_id": "u1", "width": 100, "height": 100, "file_size": len(photo_bytes)}],
                    },
                },
            },
            # Ordinary raw reaction + reaction_count + my_chat_member.
            {
                "feed_id": 13,
                "place": place_tag,
                "update": {
                    "update_id": 900000126,
                    "message_reaction": {
                        "chat": {"id": -100500, "type": "supergroup", "title": "Team Room"},
                        "message_id": 88,
                        "user": {"id": 555, "is_bot": False, "first_name": "Bob"},
                        "date": 1730000003,
                        "old_reaction": [],
                        "new_reaction": [{"type": "emoji", "emoji": "👍"}],
                    },
                },
            },
            {
                "feed_id": 14,
                "place": place_tag,
                "update": {
                    "update_id": 900000127,
                    "message_reaction_count": {
                        "chat": {"id": -100500, "type": "supergroup", "title": "Team Room"},
                        "message_id": 88,
                        "date": 1730000004,
                        "reactions": [{"type": {"type": "emoji", "emoji": "🔥"}, "total_count": 2}],
                    },
                },
            },
            {
                "feed_id": 15,
                "update": {
                    "update_id": 900000128,
                    "my_chat_member": {
                        "chat": {"id": -100501, "type": "supergroup", "title": "Joined Via Member"},
                        "from": {"id": 555, "is_bot": False, "first_name": "Bob"},
                        "date": 1730000005,
                        "old_chat_member": {"user": {"id": 1001, "is_bot": True}, "status": "left"},
                        "new_chat_member": {"user": {"id": 1001, "is_bot": True}, "status": "member"},
                    },
                },
            },
        ]
        host = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "presence_identity": 1,
                "presence_reaction_context": 1,
                "presence_explicit_completion": 1,
                "telegram_group_feed": 1,
            },
            feed_identity=lambda query: {
                **_v2_identity("1001", label="@owner_bot", enabled=True, reason=""),
                **({"group_ids": ["-100500", "-100501"]} if query.get("groups") == "1" else {}),
            },
            feed_updates=[batch_1],
            call_handler=call_handler,
            turn_handler=turn_handler,
            work_handler=work_handler,
            owner_places={
                ("1001", "-100500"): {"place_id": "p-team", "connected_revision": 2, "state": "active", "label": "Team Room"},
            },
        )
        plugin._RUNTIME.submitter.transport = host
        plugin._OWNER_RUNTIME.submitter.transport = host
        plugin._OWNER_RUNTIME.owner_feed._bytes_transport = bytes_transport

        [(_name, supervised, _opts)] = api.tasks
        task = asyncio.create_task(supervised())
        try:
            # Wait for spool ACK (second /updates query carries offset=16) AND first failed download attempt.
            await _wait_until(lambda: len(host.updates_queries) >= 2 and len(downloaded_urls) >= 1)
            assert host.updates_queries[0]["account_id"] == "1001"
            assert host.updates_queries[1]["offset"] == "16"

            # Expedite retry backoff so the post-ACK attachment download retry runs immediately.
            with plugin._OWNER_RUNTIME.store._connect() as conn:
                conn.execute("UPDATE inbox SET available_at=0 WHERE state='pending'")

            # Wait for all 3 valid events (message 88 + reaction + reaction_count) AND immediate + deferred replies.
            await _wait_until(lambda: len(host.turns) == 3)
            await _wait_until(lambda: sum(1 for c in host.feed_calls if c["method"] == "sendMessage") >= 2)

            # Verify attachment retry AFTER spool ACK kept exact wire-v2 sideband query params.
            assert len(downloaded_urls) == 2
            for dl_url in downloaded_urls:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(dl_url).query, keep_blank_values=True)
                assert qs == {
                    "account_id": ["1001"],
                    "place_id": ["p-team"],
                    "connected_revision": ["2"],
                    "thread_id": ["77"],
                    "file_path": ["photos/p1.jpg"],
                }

            # Verify migrated old group -100400 retired, Team Room & my_chat_member group listed.
            a_status = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert a_status["enabled"] is True and a_status["reason"] == ""
            assert a_status["account_id"] == "1001"
            assert a_status["connect"] == [
                {"account_id": "1001", "conversation_id": "-100501", "label": "Joined Via Member (-100501)"},
                {"account_id": "1001", "conversation_id": "-100500", "label": "Team Room (-100500)"},
            ]
            assert plugin._OWNER_RUNTIME.store.owner_feed_offset("1001") == 16
            assert plugin._RUNTIME.store.telegram_offset() == 0

            # Automatic immediate + deferred replies preserved exact bound place/rev/thread!
            auto_sends = [c for c in host.feed_calls if c["method"] == "sendMessage"]
            assert [c["params"]["text"] for c in auto_sends] == ["immediate ack", "deferred final answer"]
            for c in auto_sends:
                assert c["place_id"] == "p-team" and c["connected_revision"] == 2 and c["thread_id"] == "77"
                assert c["params"]["message_thread_id"] == 77

            # Verify reaction + reaction_count were both submitted with exact binding_id + connected_revision.
            kinds = {t["event"]["message"].get("event_kind") for t in host.turns}
            assert kinds == {"message", "message_reaction", "message_reaction_count"}

            # Proactive A outbound: text, photo, album, edit (topic recovered!), react, moderate, typing.
            send_tool = api.tools["telegram_send"][0]
            op_tool = api.tools["telegram_operation"][0]
            react_tool = api.tools["telegram_react"][0]
            mod_tool = api.tools["telegram_moderate"][0]
            receipt_tool = api.tools["telegram_receipt"][0]

            out_photo = tmp_path / "send.jpg"
            out_photo_bytes = b"\xff\xd8\xff\xdbsend-photo-payload"
            out_photo.write_bytes(out_photo_bytes)
            out_photo_2 = tmp_path / "send2.jpg"
            out_photo_2_bytes = b"\xff\xd8\xff\xdbsend-photo-payload-2"
            out_photo_2.write_bytes(out_photo_2_bytes)

            r_text = send_tool(account_id="1001", chat_id="-100500", kind="message", text="**hello** team", topic_id="77", request_id="req-text")
            assert r_text["ok"] is True and r_text["account_id"] == "1001"

            r_photo = send_tool(account_id="1001", chat_id="-100500", kind="photo", file_path=str(out_photo), caption="pic", request_id="req-photo")
            assert r_photo["ok"] is True

            r_album = send_tool(
                account_id="1001",
                chat_id="-100500",
                kind="album",
                items=[
                    {"kind": "photo", "file_path": str(out_photo), "caption": "one"},
                    {"kind": "photo", "file_path": str(out_photo_2), "caption": "two"},
                ],
                request_id="req-album",
            )
            assert r_album["ok"] is True
            await _wait_until(lambda: receipt_tool(request_id="req-album", account_id="1001").get("state") == "delivered")

            # Edit own message 401 (sent in topic 77) omitting topic_id -> tool recovers topic 77 for binding/sideband!
            r_edit = op_tool(account_id="1001", method="editMessageText", chat_id="-100500", message_id=401, text="edited", request_id="req-edit")
            assert r_edit["ok"] is True
            r_react = react_tool(account_id="1001", action="set", chat_id="-100500", message_id=88, topic_id=77, emoji="🔥", request_id="req-react")
            assert r_react["ok"] is True
            r_del = mod_tool(account_id="1001", action="delete_message", chat_id="-100500", message_id=88, request_id="req-del")
            assert r_del["ok"] is True

            await _wait_until(lambda: receipt_tool(request_id="req-del", operation="moderate", account_id="1001").get("state") == "delivered")
            assert not any(r.get("delivery_id") == "telegram-moderate:req-del" for r in host.reports)

            # Typing for an active A turn sends sendChatAction with the exact retained place/rev/thread sideband.
            typing_tool = api.tools["telegram_typing"][0]
            evt_id_88 = next(t["event"]["source_event_id"] for t in host.turns if t["event"]["message"].get("message_id") == 88 and t["event"]["message"].get("event_kind") == "message")
            with plugin._OWNER_RUNTIME.store._connect() as conn:
                conn.execute("UPDATE inbox SET state='leased', lease_until=? WHERE event_id=?", (9999999999.0, evt_id_88))
            ctx_typing = SimpleNamespace(
                task_id="turn-88",
                task_metadata={"presence": {"event": {
                    "provider": "telegram", "account_id": "1001", "conversation_id": "-100500",
                    "thread_id": "77", "source_event_id": evt_id_88,
                }}},
            )
            r_typing = typing_tool(ctx=ctx_typing, account_id="1001", chat_id="-100500", topic_id=77)
            assert r_typing["ok"] is True and r_typing["typing"] == "requested"
            assert await plugin._OWNER_RUNTIME.process_typing() == 1
            typing_calls = [c for c in host.feed_calls if c["method"] == "sendChatAction"]
            assert len(typing_calls) == 1
            assert typing_calls[0]["place_id"] == "p-team"
            assert typing_calls[0]["connected_revision"] == 2
            assert typing_calls[0]["thread_id"] == "77"
            assert typing_calls[0]["params"] == {"chat_id": "-100500", "action": "typing", "message_thread_id": 77}

            # Verify bind + check queries used exact wire-v2 contribution_id=owner_bot.
            ops = {q.get("operation") for q in host.place_queries}
            assert ops == {"bind", "check"}

            # Verify relay call/upload payloads: zero provider polling, confined upload metadata, no fake transfer progress.
            called_methods = [c["method"] for c in host.feed_calls]
            assert not ({"getUpdates", "deleteWebhook", "getMe"} & set(called_methods))
            assert {"getFile", "sendMessage", "editMessageText", "setMessageReaction", "deleteMessage"} <= set(called_methods)

            assert [u["method"] for u in host.feed_uploads] == ["sendPhoto", "sendMediaGroup"]
            photo_upload = host.feed_uploads[0]
            assert photo_upload["account_id"] == "1001"
            assert photo_upload["place_id"] == "p-team"
            assert photo_upload["connected_revision"] == 2
            assert photo_upload["thread_id"] == ""
            assert "photo" not in photo_upload["params"]  # file comes from confined files[] handle only
            f0 = photo_upload["files"][0]
            assert f0["field"] == "photo"
            assert f0["size"] == len(out_photo_bytes)
            assert f0["sha256"] == hashlib.sha256(out_photo_bytes).hexdigest()
            assert str(owner_state_dir(api.state_dir).resolve()) in str(pathlib.Path(f0["path"]).resolve())

            album_upload = host.feed_uploads[1]
            assert [m["media"] for m in album_upload["params"]["media"]] == ["attach://file0", "attach://file1"]
            assert len(album_upload["files"]) == 2

            # Factual upload progress: relay POST never fabricates receipt.transfer for A.
            photo_receipt = receipt_tool(request_id="req-photo", account_id="1001")
            assert photo_receipt["state"] == "delivered"
            assert photo_receipt["account_id"] == "1001"
            assert "transfer" not in photo_receipt
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            api.unload_cb()

    asyncio.run(run_case())


def test_concurrent_a_and_b_in_same_group_and_b_upload_progress_unchanged(tmp_path):
    async def run_case():
        api = _FakeApi(tmp_path / "state", bot_token="2002:standalone-secret")
        plugin.register(api)
        host = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "presence_identity": 1,
                "telegram_group_feed": 1,
            },
            feed_identity=_v2_identity("1001", label="@owner_bot", enabled=True, reason="", same_as_owner_bridge=False),
            feed_updates=[[
                {"feed_id": 1, "sighting": {"chat": {"id": -100500, "type": "supergroup", "title": "Shared Group"}, "membership": "present"}},
            ]],
            owner_places={
                ("1001", "-100500"): {"place_id": "p-a", "connected_revision": 1, "state": "active", "label": "Shared Group A"},
            },
        )
        tg_b = _FakeTelegramTransport(
            bot_id=2002,
            username="standalone_bot",
            updates=[[
                {
                    "update_id": 50,
                    "message": {
                        "message_id": 10,
                        "date": 1730000000,
                        "from": {"id": 9, "is_bot": False, "first_name": "U"},
                        "chat": {"id": -100500, "type": "supergroup", "title": "Shared Group"},
                        "text": "hello B",
                    },
                },
            ]],
        )
        plugin._RUNTIME.submitter.transport = host
        plugin._OWNER_RUNTIME.submitter.transport = host
        plugin._RUNTIME.client_factory = lambda token: TelegramClient(token, transport=tg_b)

        [(_name, supervised, _opts)] = api.tasks
        task = asyncio.create_task(supervised())
        try:
            await _wait_until(lambda: plugin._RUNTIME.confirmed_bot()[0] == "2002" and plugin._OWNER_RUNTIME.confirmed_bot()[0] == "1001")
            await _wait_until(lambda: plugin._RUNTIME.store.telegram_offset() == 51 and plugin._OWNER_RUNTIME.store.owner_feed_offset("1001") == 2)
            await _wait_until(lambda: len(host.turns) == 1)

            # B turn stays pure legacy: no binding_id (places_version=1) and no connected_revision.
            assert "binding_id" not in host.turns[0]
            assert "connected_revision" not in host.turns[0]

            # Queue file send on B (reports its own transfer bytes) and file send on A (no fabricated transfer bytes).
            pic = tmp_path / "pic.jpg"
            pic.write_bytes(b"\xff\xd8\xff\xdbpayload-bytes")
            send_tool = api.tools["telegram_send"][0]
            receipt_tool = api.tools["telegram_receipt"][0]
            rb = send_tool(chat_id="-100500", kind="photo", file_path=str(pic), request_id="same-req-id")
            ra = send_tool(account_id="1001", chat_id="-100500", kind="photo", file_path=str(pic), request_id="same-req-id")
            assert rb["ok"] is True and "account_id" not in rb
            assert ra["ok"] is True and ra["account_id"] == "1001"
            await _wait_until(lambda: receipt_tool(request_id="same-req-id").get("state") == "delivered"
                              and receipt_tool(request_id="same-req-id", account_id="1001").get("state") == "delivered")

            rec_b = receipt_tool(request_id="same-req-id")
            rec_a = receipt_tool(request_id="same-req-id", account_id="1001")
            assert rec_b["transfer"]["file_bytes_written"] == len(pic.read_bytes())
            assert rec_b["transfer"]["answer"] == "ok"
            assert "transfer" not in rec_a

            # B status stays pure B; owner_bot_status stays pure A.
            status_b = _json_body(await api.routes["status"](_FakeRequest()))
            status_a = _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
            assert status_b["bot_label"] == "@standalone_bot" and status_b["outbox_delivered"] == 1
            assert status_a["enabled"] is True and status_a["account_id"] == "1001"
            assert status_a["identity"]["sends_as"]["label"] == "@owner_bot"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            api.unload_cb()

    asyncio.run(run_case())


def test_reconnect_revision_change_ends_old_rows_unsent_without_rebinding_or_blocking_new_rev(tmp_path):
    async def run_case():
        api = _FakeApi(tmp_path / "state", bot_token="")
        plugin.register(api)
        host = _ScriptedHostTransport(
            identity_caps={"presence_delivery_version": 1, "places_version": 1, "telegram_group_feed": 1},
            feed_identity=_v2_identity("1001", label="@owner_bot", enabled=True, reason=""),
            owner_places={
                ("1001", "-100500"): {"place_id": "p-team", "connected_revision": 1, "state": "active", "label": "Team Room"},
            },
        )
        owner_rt = plugin._OWNER_RUNTIME
        owner_rt.submitter.transport = host
        owner_rt.owner_feed._transport = host
        owner_rt._activate_owner_client(await owner_rt.owner_feed.identity())

        send_tool = api.tools["telegram_send"][0]
        receipt_tool = api.tools["telegram_receipt"][0]

        # Queue one proactive send (topic "") and one automatic reply (topic "9") under connected_revision=1.
        r1 = send_tool(account_id="1001", chat_id="-100500", kind="message", text="queued on rev 1", request_id="rev1-proactive")
        assert r1["ok"] is True
        evt_r1 = parse_telegram_update(
            {
                "update_id": 77,
                "message": {
                    "message_id": 5,
                    "message_thread_id": 9,
                    "is_topic_message": True,
                    "date": 1730000000,
                    "from": {"id": 55, "is_bot": False, "first_name": "Bob"},
                    "chat": {"id": -100500, "type": "supergroup", "title": "Team Room", "is_forum": True},
                    "text": "hi rev 1",
                },
            },
            bot_account_id="1001",
            places=True,
            group_only=True,
        )
        assert evt_r1 is not None
        owner_rt.store.commit_update(
            77,
            evt_r1,
            advance_offset=False,
            feed_cursor=("1001", 1),
            place_binding=_binding("1001", "-100500", thread_id="9", place_id="p-team", connected_revision=1),
        )
        lease_r1 = owner_rt.store.claim_inbox()
        assert lease_r1 is not None
        owner_rt.store.record_submission(
            lease_r1.event_id,
            binding_id="p-team",
            turn_ref="turn-r1",
            outcome="message",
            text="auto reply rev 1",
            work_ref="",
            delivery_reporting_version=1,
        )

        # Disconnect & reconnect the same group place -> connected_revision advances to 2!
        host.owner_places[("1001", "-100500")]["connected_revision"] = 2

        # Queue a new proactive send under rev 2 (same account_id, same chat_id, same topic_id="" as rev1-proactive!).
        r2 = send_tool(account_id="1001", chat_id="-100500", kind="message", text="queued on rev 2", request_id="rev2-proactive")
        assert r2["ok"] is True

        # Process all three rows: the ended rev-1 connection's proactive send and auto reply end unsent (never rebound,
        # never retried for ever), and rev-2 proactive is delivered without waiting behind them.
        assert await owner_rt.process_one_outbox() is True
        assert await owner_rt.process_one_outbox() is True
        assert await owner_rt.process_one_outbox() is True
        assert await owner_rt.process_one_outbox() is False

        assert len(host.feed_calls) == 1
        assert host.feed_calls[0]["connected_revision"] == 2
        assert host.feed_calls[0]["params"]["text"] == "queued on rev 2"

        rec1 = receipt_tool(request_id="rev1-proactive", account_id="1001")
        rec2 = receipt_tool(request_id="rev2-proactive", account_id="1001")
        assert rec1["state"] == "failed" and rec1["error"].startswith("Not sent:")
        assert "never sent to the new connection" in rec1["error"]
        assert rec2["state"] == "delivered"

        counts, _card = owner_rt.store.read_status()
        assert counts["outbox_held"] == 0 and counts["outbox_failed"] == 2 and counts["outbox_uncertain"] == 0
        assert counts["outbox_waiting"] == 0  # nothing left to retry
        api.unload_cb()

    asyncio.run(run_case())


def test_proactive_bind_refusal_before_file_copy_and_reload_account_guard(tmp_path):
    api = _FakeApi(tmp_path / "state", bot_token="")
    plugin.register(api)
    host = _ScriptedHostTransport(
        identity_caps={"presence_delivery_version": 1, "places_version": 1, "telegram_group_feed": 1},
        owner_places={},  # -100500 is NOT connected!
    )
    plugin._OWNER_RUNTIME.submitter.transport = host
    CustodyStore.shared(owner_store_path(api.state_dir)).record_confirmed_owner_bot("1001", username="owner_bot")

    send_tool = api.tools["telegram_send"][0]

    # 1. Unconnected A group refuses BEFORE file copy and BEFORE outbox write!
    src_file = tmp_path / "report.txt"
    src_file.write_text("sensitive", encoding="utf-8")
    not_conn = send_tool(account_id="1001", chat_id="-100500", kind="document", file_path=str(src_file), request_id="nc-1")
    assert not_conn["ok"] is False and not_conn["code"] == "place_not_connected"
    assert not (owner_state_dir(api.state_dir) / "outbox_files").exists()
    assert CustodyStore.shared(owner_store_path(api.state_dir)).outbox_payload("telegram-send:nc-1") == {}

    # 2. Auth blocked refuses cleanly before queueing.
    host.owner_places[("1001", "-100500")] = {"place_id": "p1", "connected_revision": 1, "state": "auth_blocked"}
    auth_bad = send_tool(account_id="1001", chat_id="-100500", kind="message", text="no", request_id="auth-1")
    assert auth_bad["ok"] is False and auth_bad["code"] == "presence_auth_blocked"

    # 3. A-only setup: omitting account_id refuses cleanly with account_id_required.
    assert send_tool(chat_id="-100500", kind="message", text="hi")["code"] == "account_id_required"

    # 4. Reload preservation + turn-account leak guard when B (2002) is active and A is mid-reload!
    api.bot_token = "2002:b-secret"
    CustodyStore.shared(api.state_dir / "custody.sqlite3").record_confirmed_bot("2002:b-secret", "2002")

    # Simulate transient bridge_not_ready during reload: clears in-memory _bot_id, preserves persisted confirmed_owner_bot!
    plugin._OWNER_RUNTIME._clear_owner_identity(
        state="waiting_for_owner_bot",
        reason="bridge_not_ready",
        enabled=False,
        observed=True,
        forget_recorded=False,
    )
    assert plugin._OWNER_RUNTIME.recorded_bot() == "1001"

    # Even if confirmed_owner_bot were forgotten during an A turn, omitting account_id MUST NOT fall through to B:
    # the turn's own event in A's store says it came through A, whichever bot is recorded as active.
    store_a = CustodyStore.shared(owner_store_path(api.state_dir))
    store_a.forget_confirmed_owner_bot()
    evt_a = parse_telegram_update(
        {"update_id": 91, "message": {"message_id": 7, "date": 1730000000, "text": "hi A",
                                      "from": {"id": 55, "is_bot": False, "first_name": "Bob"},
                                      "chat": {"id": -100500, "type": "supergroup", "title": "Team"}}},
        bot_account_id="1001", places=True, group_only=True,
    )
    assert evt_a is not None
    store_a.commit_update(5, evt_a, advance_offset=False, feed_cursor=("1001", 6),
                          place_binding=_binding("1001", "-100500", place_id="p1", connected_revision=1))
    ctx_a = SimpleNamespace(
        task_id="t-a",
        task_metadata={"presence": {"event": {"provider": "telegram", "account_id": "1001", "conversation_id": "-100500",
                                              "source_event_id": evt_a.source_event_id}}},
    )
    turn_a_omitted = send_tool(ctx=ctx_a, chat_id="-100500", kind="message", text="hi")
    assert turn_a_omitted["ok"] is False and turn_a_omitted["code"] == "account_id_required"
    assert "account_id='1001'" in turn_a_omitted["error"]
    # Naming the turn's own bot while it is not the connected one: refused, never moved to another bot.
    turn_a_named = send_tool(ctx=ctx_a, account_id="1001", chat_id="-100500", kind="message", text="hi")
    assert turn_a_named["ok"] is False and turn_a_named["code"] == "account_not_active"

    # Legacy standalone B call without account_id still works with exact 0.13.1 payload (no account_id or _place_binding).
    legacy_b = send_tool(chat_id="42", kind="message", text="dm via B", request_id="legacy-b")
    assert legacy_b["ok"] is True and "account_id" not in legacy_b
    payload_b = CustodyStore.shared(api.state_dir / "custody.sqlite3").outbox_payload("telegram-send:legacy-b")
    assert "account_id" not in payload_b and "_place_binding" not in payload_b
    api.unload_cb()


def test_retained_old_a1_operations_end_unsent_after_switch_to_a2_without_blocking_a2(tmp_path):
    async def run_case():
        api = _FakeApi(tmp_path / "state", bot_token="")
        plugin.register(api)
        host = _ScriptedHostTransport(
            identity_caps={"presence_delivery_version": 1, "places_version": 1, "telegram_group_feed": 1},
            feed_identity=_v2_identity("1002", label="@new_owner", enabled=True, reason=""),
            owner_places={
                ("1001", "-100500"): {"place_id": "p-a1", "connected_revision": 1, "state": "active", "label": "Team A1"},
                ("1002", "-100500"): {"place_id": "p-a2", "connected_revision": 1, "state": "active", "label": "Team A2"},
            },
        )
        plugin._OWNER_RUNTIME.submitter.transport = host
        store_a = CustodyStore.shared(owner_store_path(api.state_dir))
        store_a.record_confirmed_owner_bot("1001", username="old_owner")

        send_tool = api.tools["telegram_send"][0]
        receipt_tool = api.tools["telegram_receipt"][0]
        r_a1 = send_tool(account_id="1001", chat_id="-100500", kind="message", text="queued for A1", request_id="old-a1")
        assert r_a1["ok"] is True

        evt_a1 = parse_telegram_update(
            {
                "update_id": 77,
                "message": {
                    "message_id": 5,
                    "message_thread_id": 9,
                    "is_topic_message": True,
                    "date": 1730000000,
                    "from": {"id": 55, "is_bot": False, "first_name": "Bob"},
                    "chat": {"id": -100500, "type": "supergroup", "title": "Team", "is_forum": True},
                    "text": "ping A1",
                },
            },
            bot_account_id="1001",
            places=True,
            group_only=True,
        )
        assert evt_a1 is not None
        b_a1 = _binding("1001", "-100500", thread_id="9", place_id="p-a1", connected_revision=1)
        store_a.commit_update(77, evt_a1, advance_offset=False, feed_cursor=("1001", 1), place_binding=b_a1)
        lease_a1 = store_a.claim_inbox()
        assert lease_a1 is not None
        store_a.record_submission(
            lease_a1.event_id,
            binding_id="p-a1",
            turn_ref="turn-a1",
            outcome="message",
            text="auto reply from A1",
            work_ref="",
            delivery_reporting_version=1,
        )
        assert store_a.outbox_payload(f"presence:{lease_a1.event_id}:turn")["account_id"] == "1001"
        assert store_a.outbox_payload(f"presence:{lease_a1.event_id}:turn")["_place_binding"] == b_a1
        store_a.request_typing(
            chat_id="-100500",
            topic_id="9",
            turn_ref="t1",
            source_event_id="telegram:1001:5",
            place_binding=b_a1,
        )

        # Switch active owner bot to A2 (1002).
        owner_rt = plugin._OWNER_RUNTIME
        owner_rt.owner_feed._transport = host
        owner_rt._activate_owner_client(await owner_rt.owner_feed.identity())

        r_a2 = send_tool(account_id="1002", chat_id="-100500", kind="message", text="queued for A2", request_id="new-a2")
        assert r_a2["ok"] is True

        assert await owner_rt.process_one_outbox() is True  # A1 tool row -> ends unsent (its bot is no longer A)
        assert await owner_rt.process_one_outbox() is True  # A1 auto reply -> ends unsent, never retargeted to A2
        assert await owner_rt.process_one_outbox() is True  # A2 row -> DELIVERED (not blocked by A1!)
        assert await owner_rt.process_one_outbox() is False  # nothing of A1 loops
        assert await owner_rt.process_typing() == 0  # A1 typing dropped without calling A2!

        assert [c["method"] for c in host.feed_calls] == ["sendMessage"]
        assert host.feed_calls[0]["account_id"] == "1002"
        assert host.feed_calls[0]["place_id"] == "p-a2"
        assert host.feed_calls[0]["params"]["text"] == "queued for A2"

        rec_a1 = receipt_tool(request_id="old-a1", account_id="1001")
        assert rec_a1["state"] == "failed" and rec_a1["account_id"] == "1001"
        assert "queued for bot 1001" in rec_a1["error"] and "never sent through another bot" in rec_a1["error"]
        assert receipt_tool(request_id="old-a1", account_id="1002") == {"state": "not_found"}

        rec_a2 = receipt_tool(request_id="new-a2", account_id="1002")
        assert rec_a2["state"] == "delivered" and rec_a2["account_id"] == "1002"

        counts, _card = store_a.read_status()
        assert counts["outbox_held"] == 0 and counts["outbox_failed"] == 2
        api.unload_cb()

    asyncio.run(run_case())


def test_same_bot_collision_v2_true_false_null_and_old_core_no_probe(tmp_path):
    async def run_case():
        # Case 1: same_as_owner_bridge=True even while A feed is OFF -> zero Telegram calls!
        host_1 = _ScriptedHostTransport(
            identity_caps={"presence_delivery_version": 1, "places_version": 1, "telegram_group_feed": 1},
            feed_identity=lambda q: _v2_identity(
                "1001",
                enabled=False,
                reason="feed_off",
                same_as_owner_bridge=(q.get("bot_id") == "1001"),
                include_identity=False,
            ),
        )
        tg_1 = _FakeTelegramTransport(bot_id=1001, username="owner_bot")
        submitter_1 = PresenceHostClient(state_dir=tmp_path / "c1", token_provider=lambda: "tok", host_base="http://127.0.0.1:1", transport=host_1)
        await submitter_1.discover_delivery_support()
        feed_1 = OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "tok", transport=host_1)
        rt_1 = TelegramTransportRuntime(
            state_dir=tmp_path / "c1",
            token_provider=lambda: "1001:owner-secret",
            logger=None,
            submitter=submitter_1,
            client_factory=lambda t: TelegramClient(t, transport=tg_1),
            owner_feed=feed_1,
        )
        rt_1._run_task = asyncio.current_task()
        assert await rt_1._ready_client() is None
        assert tg_1.calls == []
        assert rt_1._runtime_state == "conflict_with_owner_bot"
        assert rt_1.confirmed_bot() == ("", BOT_CONFLICT_WITH_OWNER)
        rt_1.store.close()

        # Case 2: non-prefix token whose getMe returns A's id -> getMe only, ZERO deleteWebhook/getUpdates.
        tg_2 = _FakeTelegramTransport(bot_id=1001, username="owner_bot")
        rt_2 = TelegramTransportRuntime(
            state_dir=tmp_path / "c2",
            token_provider=lambda: "opaque-token-without-colon",
            logger=None,
            submitter=submitter_1,
            client_factory=lambda t: TelegramClient(t, transport=tg_2),
            owner_feed=feed_1,
        )
        rt_2._run_task = asyncio.current_task()
        assert await rt_2._ready_client() is None
        assert [m for m, _ in tg_2.calls] == ["getMe"]
        assert rt_2._runtime_state == "conflict_with_owner_bot"
        rt_2.store.close()

        # Case 3: wire v2: same_as_owner_bridge=None stays unknown -> wait (even with reason=no_token!);
        # same_as_owner_bridge=False (native provably disabled/unconfigured) -> clear!
        race_state = {"same": None, "reason": "no_token"}
        host_3 = _ScriptedHostTransport(
            identity_caps={"presence_delivery_version": 1, "places_version": 1, "telegram_group_feed": 1},
            feed_identity=lambda _q: _v2_identity(
                "",
                enabled=False,
                reason=race_state["reason"],
                same_as_owner_bridge=race_state["same"],
                include_identity=False,
            ),
        )
        tg_3 = _FakeTelegramTransport(bot_id=2002, username="b_bot")
        submitter_3 = PresenceHostClient(state_dir=tmp_path / "c3", token_provider=lambda: "tok", host_base="http://127.0.0.1:1", transport=host_3)
        await submitter_3.discover_delivery_support()
        feed_3 = OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "tok", transport=host_3)
        rt_3 = TelegramTransportRuntime(
            state_dir=tmp_path / "c3",
            token_provider=lambda: "2002:b-secret",
            logger=None,
            submitter=submitter_3,
            client_factory=lambda t: TelegramClient(t, transport=tg_3),
            owner_feed=feed_3,
        )
        assert await rt_3._ready_client() is None
        assert tg_3.calls == []  # None stays unknown, never bypasses!
        race_state["same"] = False
        assert await rt_3._ready_client() is not None
        assert [m for m, _ in tg_3.calls] == ["getMe", "deleteWebhook"]
        assert host_1.poller_claims == [] and host_3.poller_claims == []
        rt_3.store.close()

        # Case 4: old core (no telegram_group_feed) -> ZERO /telegram/feed/identity or /poller probes, no hold.
        host_old = _ScriptedHostTransport(identity_caps={"presence_delivery_version": 1, "places_version": 1})
        tg_old = _FakeTelegramTransport(bot_id=2002, username="b_bot")
        sub_old = PresenceHostClient(state_dir=tmp_path / "c4", token_provider=lambda: "tok", host_base="http://127.0.0.1:1", transport=host_old)
        await sub_old.discover_delivery_support()
        feed_old = OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "tok", transport=host_old)
        rt_old = TelegramTransportRuntime(
            state_dir=tmp_path / "c4",
            token_provider=lambda: "2002:b-secret",
            logger=None,
            submitter=sub_old,
            client_factory=lambda t: TelegramClient(t, transport=tg_old),
            owner_feed=feed_old,
        )
        assert await rt_old._ready_client() is not None
        assert host_old.identity_queries == []
        assert host_old.poller_claims == []
        assert [m for m, _ in tg_old.calls] == ["deleteWebhook", "getMe"]
        rt_old.store.close()

    asyncio.run(run_case())


def test_relay_v2_outcomes_holds_and_known_refusals(tmp_path):
    async def run_case():
        state_dir = tmp_path / "owner_state"
        state_dir.mkdir(parents=True)

        responses: List[Any] = []
        calls_seen: List[Dict[str, Any]] = []

        class _RelayStub:
            async def request_json(
                self,
                method: str,
                url: str,
                *,
                headers: Dict[str, str],
                payload: Optional[Dict[str, Any]],
                timeout_sec: float,
            ) -> Dict[str, Any]:
                calls_seen.append(dict(payload or {}))
                nxt = responses.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt

        bnd = _binding("1001", "-100500", thread_id="", place_id="p-team", connected_revision=3)
        unbound_relay = RelayTelegramTransport(
            account_id="1001",
            host_base="http://127.0.0.1:1",
            token_provider=lambda: "tok",
            allowed_files_root=state_dir,
            host_transport=_RelayStub(),
        )
        bound_relay = unbound_relay.with_binding(bnd)
        unbound_client = TelegramClient("relay-account-1001", transport=unbound_relay, api_server=RELAY_API_SERVER)
        bound_client = TelegramClient("relay-account-1001", transport=bound_relay, api_server=RELAY_API_SERVER)

        submitter = SimpleNamespace(
            places_version=1,
            delivery_reporting_status="supported",
            presence_explicit_completion=1,
            discover_delivery_support=None,
            place_hold=None,
            check_owner_place=None,
            report_delivery=None,
        )
        rt = TelegramTransportRuntime(
            state_dir=state_dir,
            token_provider=lambda: "",
            logger=None,
            submitter=submitter,
            source_mode=SOURCE_OWNER_BOT,
        )
        rt._client = unbound_client
        rt._bot_id = "1001"
        rt._feed_enabled = True

        # 1. 502 relay_outcome=unknown -> UNCERTAIN and NEVER auto-resent.
        responses.append(PresenceHostHTTPError(502, "unknown", {"relay_outcome": "unknown"}))
        rt.store.enqueue_outbox("telegram-send:uncertain-1", {
            "kind": "message", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": bnd,
            "text": "once only", "markdown": False,
            "_rendered_chunks": [{"text": "once only", "parse_mode": ""}],
            "_reporting": {"version": 0},
        })
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-send:uncertain-1")["state"] == "uncertain"
        assert len(calls_seen) == 1
        assert await rt.process_one_outbox() is False

        # 2. 503 relay_outcome=not_sent -> clears CALL_STARTED and retries cleanly.
        calls_seen.clear()
        responses.extend([
            PresenceHostHTTPError(503, "not sent", {"relay_outcome": "not_sent"}),
            {"ok": True, "result": {"message_id": 801, "chat": {"id": -100500, "type": "supergroup"}}},
        ])
        rt.store.enqueue_outbox("telegram-send:retry-1", {
            "kind": "message", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": bnd,
            "text": "safe retry", "markdown": False,
            "_rendered_chunks": [{"text": "safe retry", "parse_mode": ""}],
            "_reporting": {"version": 0},
        })
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-send:retry-1")["state"] == "pending"
        with rt.store._connect() as conn:
            conn.execute("UPDATE outbox SET available_at=0 WHERE delivery_id='telegram-send:retry-1'")
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-send:retry-1")["state"] == "delivered"
        assert len(calls_seen) == 2

        # 3. Pre-effect 403 hold refusals (including busy + auth_unavailable + feed_unavailable)
        #    hold cleanly with typed kind & retry_after hint, WITHOUT consuming ordinary retry budget (attempts==0).
        hold_cases = [
            ("place_not_active", "", None),
            ("not_connected", HOLD_PLACE_NOT_CONNECTED, None),
            ("feed_off", "", None),
            ("account_mismatch", HOLD_ACCOUNT_MISMATCH, None),
            ("bridge_not_ready", "", 5),
            ("busy", HOLD_RELAY_BUSY, 1),
            ("auth_unavailable", HOLD_AUTH_UNAVAILABLE, 5),
            ("feed_unavailable", "", 5),
        ]
        for idx, (code, expected_kind, hint) in enumerate(hold_cases):
            did = f"telegram-send:hold-{idx}"
            responses.append(PresenceHostHTTPError(403, code, {
                "ok": False, "error_code": 403, "description": f"refused {code}",
                "relay_refusal": code,
                **({"parameters": {"retry_after": hint}} if hint is not None else {}),
            }))
            rt.store.enqueue_outbox(did, {
                "kind": "message", "chat_id": "-100500", "account_id": "1001",
                "_place_binding": _binding("1001", "-100500", place_id=f"p-hold-{idx}", connected_revision=1),
                "text": code, "markdown": False,
                "_rendered_chunks": [{"text": code, "parse_mode": ""}],
                "_reporting": {"version": 0},
            })
            before = time.time()
            assert await rt.process_one_outbox() is True
            rec = rt.store.delivery_receipt(did)
            assert rec["state"] == "held" and rec["attempts"] == 0 and rec["error"].startswith("Held:")
            assert CALL_STARTED not in rt.store.outbox_payload(did)
            if code == "busy":
                with rt.store._connect() as conn:
                    avail, hold_kind = conn.execute(
                        "SELECT available_at, hold_kind FROM outbox WHERE delivery_id=?", (did,)
                    ).fetchone()
                assert hold_kind == HOLD_RELAY_BUSY
                assert 0.5 <= (avail - before) <= 3.0  # honors 1s busy hint instead of inflating to 5s
            if expected_kind:
                _, card = rt.store.read_status()
                matching = [r for r in card["held_reasons"] if r["reason"] == f"refused {code}"]
                assert matching and matching[0].get("kind") == expected_kind
            # Keep every held row from waking up ahead of later test rows on slow disks.
            with rt.store._connect() as conn:
                conn.execute("UPDATE outbox SET available_at=? WHERE delivery_id=?", (time.time() + 300.0, did))

        # 4. Definitive pre-effect no-send 403 refusals (including file_missing, file_changed,
        #    auth_blocked, connector_mismatch, and binding_changed: that connection ended and never comes back)
        #    fail terminally and stay distinct from uncertain.
        terminal_codes = (
            "method_not_allowed",
            "argument_not_allowed",
            "file_not_offered",
            "file_missing",
            "file_changed",
            "auth_blocked",
            "connector_mismatch",
            "binding_changed",
        )
        for idx, code in enumerate(terminal_codes):
            did = f"telegram-send:term-{idx}"
            responses.append(PresenceHostHTTPError(403, code, {
                "ok": False, "error_code": 403, "description": f"terminal {code}",
                "relay_refusal": code,
            }))
            rt.store.enqueue_outbox(did, {
                "kind": "message", "chat_id": "-100500", "account_id": "1001",
                "_place_binding": _binding("1001", "-100500", place_id=f"p-term-{idx}", connected_revision=1),
                "text": code, "markdown": False,
                "_rendered_chunks": [{"text": code, "parse_mode": ""}],
                "_reporting": {"version": 0},
            })
            assert await rt.process_one_outbox() is True
            rec = rt.store.delivery_receipt(did)
            assert rec["state"] == "failed" and rec["attempts"] == 1
            assert CALL_STARTED not in rt.store.outbox_payload(did)

        # 5. 403 relay_refusal=bridge_not_ready on reaction HOLDS (preserving attempts==0) vs real Telegram 403 -> failed.
        responses.extend([
            PresenceHostHTTPError(403, "bridge starting", {
                "ok": False, "error_code": 403, "description": "bridge starting",
                "relay_refusal": "bridge_not_ready", "parameters": {"retry_after": 5},
            }),
            {"ok": True, "result": True},
            PresenceHostHTTPError(403, "Forbidden: bot was kicked", {
                "ok": False, "error_code": 403, "description": "Forbidden: bot was kicked",
            }),
        ])
        b_react_1 = _binding("1001", "-100500", place_id="p-react-1", connected_revision=1)
        rt.store.enqueue_outbox("telegram-operation:react-relay-hold", {
            "kind": "operation", "method": "setMessageReaction", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": b_react_1,
            "message_id": 12,
            "parameters": {"chat_id": "-100500", "message_id": 12, "reaction": [{"type": "emoji", "emoji": "👍"}], "is_big": False},
            "_reporting": {"version": 0},
        })
        assert await rt.process_one_outbox() is True
        rec_rh = rt.store.delivery_receipt("telegram-operation:react-relay-hold")
        assert rec_rh["state"] == "held" and rec_rh["attempts"] == 0
        with rt.store._connect() as conn:
            conn.execute("UPDATE outbox SET available_at=0 WHERE delivery_id='telegram-operation:react-relay-hold'")
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-operation:react-relay-hold")["state"] == "delivered"

        b_react_2 = _binding("1001", "-100500", place_id="p-react-2", connected_revision=1)
        rt.store.enqueue_outbox("telegram-operation:react-tg-403", {
            "kind": "operation", "method": "setMessageReaction", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": b_react_2,
            "message_id": 13,
            "parameters": {"chat_id": "-100500", "message_id": 13, "reaction": [{"type": "emoji", "emoji": "👍"}], "is_big": False},
            "_reporting": {"version": 0},
        })
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-operation:react-tg-403")["state"] == "failed"

        # 6. Real Telegram 400 HTML refusal falls back to plain text through bound relay with unchanged sideband.
        calls_seen.clear()
        responses.extend([
            PresenceHostHTTPError(400, "parse", {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}),
            {"ok": True, "result": {"message_id": 802, "chat": {"id": -100500, "type": "supergroup"}}},
        ])
        res = await bound_client.send_text_chunk("-100500", "<b>hi</b>", parse_mode="HTML")
        assert res["message_id"] == 802
        assert len(calls_seen) == 2
        assert calls_seen[1]["params"]["text"] == "hi" and "parse_mode" not in calls_seen[1]["params"]
        assert calls_seen[1]["place_id"] == "p-team" and calls_seen[1]["connected_revision"] == 3

        # 7. Pre-effect transient relay holds (bridge_not_ready, busy, auth_unavailable) during download do not count as file failures.
        for idx, (code, hint) in enumerate((("bridge_not_ready", 5), ("busy", 1), ("auth_unavailable", 5))):
            note = rt._bounded_failure(
                f"evt-dl-{idx}",
                0,
                RelayRefusalError("getFile", code, relay_refusal=code, retry_after=hint),
            )
            assert note is None
            assert rt.store.download_failure(f"evt-dl-{idx}", 0)["own_failures"] == 0
        rt.store.close()

    asyncio.run(run_case())


def test_owner_bot_edit_and_moderation_once_only_receipts_and_restart_recovery(tmp_path):
    async def run_case():
        state_dir = tmp_path / "owner_state"
        state_dir.mkdir(parents=True)
        responses: List[Any] = []
        calls_seen: List[Dict[str, Any]] = []
        reports_sent: List[Dict[str, Any]] = []

        class _RelayStub:
            async def request_json(
                self,
                method: str,
                url: str,
                *,
                headers: Dict[str, str],
                payload: Optional[Dict[str, Any]],
                timeout_sec: float,
            ) -> Dict[str, Any]:
                calls_seen.append(dict(payload or {}))
                nxt = responses.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt

        relay = RelayTelegramTransport(
            account_id="1001",
            host_base="http://127.0.0.1:1",
            token_provider=lambda: "tok",
            allowed_files_root=state_dir,
            host_transport=_RelayStub(),
        )
        relay_client = TelegramClient("relay-account-1001", transport=relay, api_server=RELAY_API_SERVER)

        async def _report_delivery(payload: Dict[str, Any]) -> None:
            reports_sent.append(dict(payload))

        def _make_runtime() -> TelegramTransportRuntime:
            submitter = SimpleNamespace(
                places_version=1,
                delivery_reporting_status="supported",
                presence_explicit_completion=1,
                discover_delivery_support=None,
                place_hold=None,
                check_owner_place=None,
                report_delivery=_report_delivery,
            )
            rt = TelegramTransportRuntime(
                state_dir=state_dir,
                token_provider=lambda: "",
                logger=None,
                submitter=submitter,
                source_mode=SOURCE_OWNER_BOT,
            )
            rt._client = relay_client
            rt._bot_id = "1001"
            rt._feed_enabled = True
            return rt

        rt = _make_runtime()
        bnd = _binding("1001", "-100500", thread_id="77", place_id="p-team", connected_revision=2)

        # 1. A moderation lost relay reply (502 unknown) -> UNCERTAIN, NEVER retries, ZERO speech/delivery report.
        responses.append(PresenceHostHTTPError(502, "unknown", {"relay_outcome": "unknown"}))
        rt.store.enqueue_outbox("telegram-moderate:mod-lost", {
            "kind": "moderation", "chat_id": "-100500", "account_id": "1001",
            "topic_id": "77", "_place_binding": bnd,
            "action": "delete_message", "parameters": {"chat_id": "-100500", "message_id": 51},
        })
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-moderate:mod-lost")["state"] == "uncertain"
        assert len(calls_seen) == 1 and calls_seen[0]["method"] == "deleteMessage"
        assert await rt.process_one_outbox() is False
        assert rt.store.next_delivery_report() is None

        # 2. A moderation interrupted after CALL_STARTED (restart / expired lease) -> UNCERTAIN with ZERO provider calls.
        calls_seen.clear()
        rt.store.enqueue_outbox("telegram-moderate:mod-restart", {
            "kind": "moderation", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": _binding("1001", "-100500", place_id="p-mod-2", connected_revision=2),
            "action": "restrict_member",
            "parameters": {"chat_id": "-100500", "user_id": 77, "permissions": {"can_send_messages": False},
                           "use_independent_chat_permissions": True, "until_date": 0},
            CALL_STARTED: {"part": 0, "method": "restrictChatMember"},
        })
        with rt.store._connect() as conn:
            conn.execute(
                "UPDATE outbox SET state='leased', lease_until=1, attempts=1 WHERE delivery_id='telegram-moderate:mod-restart'"
            )
        rt.store.close()
        rt = _make_runtime()
        assert await rt.process_one_outbox() is True
        rec_mod_restart = rt.store.delivery_receipt("telegram-moderate:mod-restart")
        assert rec_mod_restart["state"] == "uncertain"
        assert calls_seen == []
        assert rt.store.next_delivery_report() is None

        # 3. A moderation with recorded provider success recovered before mark_delivered -> settles DELIVERED without a second call, ZERO report.
        rt.store.enqueue_outbox("telegram-moderate:mod-recorded", {
            "kind": "moderation", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": _binding("1001", "-100500", place_id="p-mod-3", connected_revision=2),
            "action": "delete_message", "parameters": {"chat_id": "-100500", "message_id": 52},
            "_moderation_result": True,
        })
        with rt.store._connect() as conn:
            conn.execute(
                "UPDATE outbox SET state='leased', lease_until=1, attempts=1 WHERE delivery_id='telegram-moderate:mod-recorded'"
            )
        assert await rt.process_one_outbox() is True
        rec_mod_ok = rt.store.delivery_receipt("telegram-moderate:mod-recorded")
        assert rec_mod_ok["state"] == "delivered"
        assert rec_mod_ok["provider_receipt"] == {"kind": "moderation", "action": "delete_message", "result": True}
        assert calls_seen == []
        assert rt.store.next_delivery_report() is None

        # 4. A moderation definitely-unsent (403 busy, 403 auth_unavailable, 503 not_sent) clears CALL_STARTED,
        #    holds without burning retries, and delivers cleanly once.
        responses.extend([
            PresenceHostHTTPError(403, "busy", {"ok": False, "error_code": 403, "description": "busy",
                                                "relay_refusal": "busy", "parameters": {"retry_after": 1}}),
            PresenceHostHTTPError(403, "auth", {"ok": False, "error_code": 403, "description": "auth check unavailable",
                                                "relay_refusal": "auth_unavailable", "parameters": {"retry_after": 5}}),
            PresenceHostHTTPError(503, "not sent", {"relay_outcome": "not_sent"}),
            {"ok": True, "result": True},
        ])
        rt.store.enqueue_outbox("telegram-moderate:mod-safe-retry", {
            "kind": "moderation", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": _binding("1001", "-100500", place_id="p-mod-4", connected_revision=2),
            "action": "delete_message", "parameters": {"chat_id": "-100500", "message_id": 53},
        })
        assert await rt.process_one_outbox() is True  # busy hold
        assert rt.store.delivery_receipt("telegram-moderate:mod-safe-retry")["state"] == "held"
        assert rt.store.delivery_receipt("telegram-moderate:mod-safe-retry")["attempts"] == 0
        assert CALL_STARTED not in rt.store.outbox_payload("telegram-moderate:mod-safe-retry")
        with rt.store._connect() as conn:
            conn.execute("UPDATE outbox SET available_at=0 WHERE delivery_id='telegram-moderate:mod-safe-retry'")
        assert await rt.process_one_outbox() is True  # auth_unavailable hold
        assert rt.store.delivery_receipt("telegram-moderate:mod-safe-retry")["state"] == "held"
        assert rt.store.delivery_receipt("telegram-moderate:mod-safe-retry")["attempts"] == 0
        with rt.store._connect() as conn:
            conn.execute("UPDATE outbox SET available_at=0 WHERE delivery_id='telegram-moderate:mod-safe-retry'")
        assert await rt.process_one_outbox() is True  # 503 not_sent -> pending retry
        assert rt.store.delivery_receipt("telegram-moderate:mod-safe-retry")["state"] == "pending"
        assert CALL_STARTED not in rt.store.outbox_payload("telegram-moderate:mod-safe-retry")
        with rt.store._connect() as conn:
            conn.execute("UPDATE outbox SET available_at=0 WHERE delivery_id='telegram-moderate:mod-safe-retry'")
        assert await rt.process_one_outbox() is True  # delivered
        assert rt.store.delivery_receipt("telegram-moderate:mod-safe-retry")["state"] == "delivered"
        assert len(calls_seen) == 4
        assert rt.store.next_delivery_report() is None

        # 5. A moderation definitive no-send (auth_blocked) fails terminally (distinct from uncertain), ZERO report.
        calls_seen.clear()
        responses.append(PresenceHostHTTPError(403, "auth blocked", {
            "ok": False, "error_code": 403, "description": "token revoked", "relay_refusal": "auth_blocked",
        }))
        rt.store.enqueue_outbox("telegram-moderate:mod-auth-blocked", {
            "kind": "moderation", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": _binding("1001", "-100500", place_id="p-mod-5", connected_revision=2),
            "action": "delete_message", "parameters": {"chat_id": "-100500", "message_id": 54},
        })
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-moderate:mod-auth-blocked")["state"] == "failed"
        assert rt.store.next_delivery_report() is None

        # 6. A editMessageText: lost reply -> uncertain; restart with CALL_STARTED -> uncertain (0 calls);
        #    recorded _operation_result -> settles delivered (0 calls); 503 not_sent -> retries & delivers with non-speech report.
        calls_seen.clear()
        responses.append(PresenceHostHTTPError(502, "unknown", {"relay_outcome": "unknown"}))
        rt.store.enqueue_outbox("telegram-operation:edit-lost", {
            "kind": "operation", "method": "editMessageText", "chat_id": "-100500", "account_id": "1001",
            "topic_id": "77", "_place_binding": bnd, "text": "updated",
            "parameters": {"chat_id": "-100500", "message_id": 401, "text": "updated"},
            "_reporting": {"version": 1, "account_id": "1001", "origin": {"kind": "tool"}},
        })
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-operation:edit-lost")["state"] == "uncertain"
        assert len(calls_seen) == 1

        calls_seen.clear()
        rt.store.enqueue_outbox("telegram-operation:edit-restart", {
            "kind": "operation", "method": "editMessageText", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": _binding("1001", "-100500", place_id="p-edit-2", connected_revision=2),
            "text": "updated 2",
            "parameters": {"chat_id": "-100500", "message_id": 402, "text": "updated 2"},
            "_reporting": {"version": 1, "account_id": "1001", "origin": {"kind": "tool"}},
            CALL_STARTED: {"part": 0, "method": "editMessageText"},
        })
        with rt.store._connect() as conn:
            conn.execute(
                "UPDATE outbox SET state='leased', lease_until=1, attempts=1 WHERE delivery_id='telegram-operation:edit-restart'"
            )
        rt.store.close()
        rt = _make_runtime()
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-operation:edit-restart")["state"] == "uncertain"
        assert calls_seen == []

        rt.store.enqueue_outbox("telegram-operation:edit-recorded", {
            "kind": "operation", "method": "editMessageText", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": _binding("1001", "-100500", place_id="p-edit-3", connected_revision=2),
            "text": "already edited",
            "parameters": {"chat_id": "-100500", "message_id": 403, "text": "already edited"},
            "_reporting": {"version": 1, "account_id": "1001", "origin": {"kind": "tool"}},
            "_operation_result": {"message_id": 403, "chat": {"id": -100500, "type": "supergroup"}, "text": "already edited"},
        })
        with rt.store._connect() as conn:
            conn.execute(
                "UPDATE outbox SET state='leased', lease_until=1, attempts=1 WHERE delivery_id='telegram-operation:edit-recorded'"
            )
        assert await rt.process_one_outbox() is True
        rec_edit_ok = rt.store.delivery_receipt("telegram-operation:edit-recorded")
        assert rec_edit_ok["state"] == "delivered"
        assert rec_edit_ok["provider_receipt"]["result"]["message_id"] == 403
        assert calls_seen == []

        responses.extend([
            PresenceHostHTTPError(503, "not sent", {"relay_outcome": "not_sent"}),
            {"ok": True, "result": {"message_id": 404, "chat": {"id": -100500, "type": "supergroup"}, "text": "retried edit"}},
        ])
        rt.store.enqueue_outbox("telegram-operation:edit-retry", {
            "kind": "operation", "method": "editMessageText", "chat_id": "-100500", "account_id": "1001",
            "_place_binding": _binding("1001", "-100500", place_id="p-edit-4", connected_revision=2),
            "text": "retried edit",
            "parameters": {"chat_id": "-100500", "message_id": 404, "text": "retried edit"},
            "_reporting": {"version": 1, "account_id": "1001", "origin": {"kind": "tool"}},
        })
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-operation:edit-retry")["state"] == "pending"
        with rt.store._connect() as conn:
            conn.execute("UPDATE outbox SET available_at=0 WHERE delivery_id='telegram-operation:edit-retry'")
        assert await rt.process_one_outbox() is True
        assert rt.store.delivery_receipt("telegram-operation:edit-retry")["state"] == "delivered"
        assert len(calls_seen) == 2

        # Flush delivery reports: every report is for editMessageText (text=="" non-speech); ZERO for moderation.
        while await rt.process_one_delivery_report():
            pass
        assert reports_sent
        assert all(not r["delivery_id"].startswith("telegram-moderate:") for r in reports_sent)
        assert all(r["text"] == "" and r["message"]["method"] == "editMessageText" for r in reports_sent)
        rt.store.close()

    asyncio.run(run_case())


def test_poll_claim_v1_normal_lifecycle_passive_reads_and_no_release_on_stop(tmp_path):
    async def run_case():
        api = _FakeApi(tmp_path / "state", bot_token="2002:standalone-secret", skill_token="claim-skill-tok")
        plugin.register(api)
        events: List[str] = []

        def poller_handler(payload: Optional[Dict[str, Any]], headers: Dict[str, str]) -> Dict[str, Any]:
            assert headers.get("X-Skill-Token") == "claim-skill-tok"
            assert payload == {"bot_id": "2002"}
            events.append("poller_claim:2002")
            return {"ok": True, "owned": True}

        host = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "presence_identity": 1,
                "telegram_group_feed": 1,
                "telegram_group_poll_claim": 1,
            },
            feed_identity=_v2_identity("1001", label="@owner_bot", enabled=True, reason=""),
            poller_handler=poller_handler,
        )

        class _OrderedTelegram(_FakeTelegramTransport):
            async def post_json(self, url: str, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
                method = url.rsplit("/", 1)[-1]
                events.append(f"tg:{method}")
                return await super().post_json(url, payload, timeout_sec)

        tg_b = _OrderedTelegram(bot_id=2002, username="standalone_bot")
        plugin._RUNTIME.submitter.transport = host
        plugin._OWNER_RUNTIME.submitter.transport = host
        plugin._RUNTIME.client_factory = lambda token: TelegramClient(token, transport=tg_b)

        # 1. Passive reads (status, owner_bot_status, get_settings) claim NOTHING.
        assert api.get_settings(["TELEGRAM_PUBLIC_BOT_TOKEN"]) == {"TELEGRAM_PUBLIC_BOT_TOKEN": "2002:standalone-secret"}
        _json_body(await api.routes["status"](_FakeRequest()))
        _json_body(await api.routes["owner_bot_status"](_FakeRequest()))
        assert host.poller_claims == []
        assert tg_b.calls == []

        # 2. Running B claims right before initial deleteWebhook and before every getUpdates; A never claims.
        [(_name, supervised, _opts)] = api.tasks
        task = asyncio.create_task(supervised())
        try:
            await _wait_until(
                lambda: events.count("tg:getUpdates") >= 2 and plugin._OWNER_RUNTIME.confirmed_bot()[0] == "1001"
            )
            assert plugin._RUNTIME.submitter.telegram_group_poll_claim == 1
            assert plugin._RUNTIME.confirmed_bot()[0] == "2002"
            # Causal order: getMe -> claim -> deleteWebhook -> claim -> getUpdates -> claim -> getUpdates
            assert events[:7] == [
                "tg:getMe",
                "poller_claim:2002",
                "tg:deleteWebhook",
                "poller_claim:2002",
                "tg:getUpdates",
                "poller_claim:2002",
                "tg:getUpdates",
            ]
            # B never uses GET /telegram/feed/identity?bot_id=... when telegram_group_poll_claim: 1 is advertised.
            assert all("bot_id" not in q for q in host.identity_queries)
            # Only canonical B ("2002") was ever claimed — never A ("1001").
            assert all(body == {"bot_id": "2002"} for _hdrs, body in host.poller_claims)
            claims_before_stop = len(host.poller_claims)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            api.unload_cb()

        # 3. Cancellation / unload sends NO release or transfer POST.
        assert len(host.poller_claims) == claims_before_stop
        assert not any(
            method == "POST" and path == "/telegram/feed/poller" and body != {"bot_id": "2002"}
            for method, path, body in host.requests
        )

    asyncio.run(run_case())


def test_poll_claim_v1_second_owner_typed_conflict_mismatch_and_no_observational_fallback(tmp_path):
    async def run_case():
        # Case 1: Second owner (owned=False) -> typed conflict before deleteWebhook, zero deleteWebhook/getUpdates,
        # zero GET /telegram/feed/identity fallback, and steady conflict avoids repeating getMe.
        api_1 = _FakeApi(tmp_path / "c1", bot_token="1001:same-as-owner", skill_token="tok-1")
        plugin.register(api_1)
        host_1 = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "telegram_group_feed": 1,
                "telegram_group_poll_claim": 1,
            },
            feed_identity=lambda _q: (_ for _ in ()).throw(AssertionError("must not fall back to GET identity")),
            poller_handler=lambda payload, _hdrs: {"ok": True, "owned": False, "holder": "telegram"},
        )
        tg_1 = _FakeTelegramTransport(bot_id=1001, username="owner_bot")
        plugin._RUNTIME.submitter.transport = host_1
        plugin._RUNTIME.client_factory = lambda token: TelegramClient(token, transport=tg_1)
        plugin._RUNTIME._run_task = asyncio.current_task()
        try:
            assert await plugin._RUNTIME._ready_client() is None
            assert [m for m, _ in tg_1.calls] == ["getMe"]  # zero deleteWebhook / getUpdates
            assert len(host_1.poller_claims) == 1 and host_1.poller_claims[0][1] == {"bot_id": "1001"}
            assert host_1.identity_queries == []
            assert plugin._RUNTIME._runtime_state == "conflict_with_owner_bot"
            assert plugin._RUNTIME.confirmed_bot() == ("", BOT_CONFLICT_WITH_OWNER)
            status_1 = _json_body(await api_1.routes["status"](_FakeRequest()))
            assert status_1["link"] == "conflict_with_owner_bot"
            assert status_1["tone"] == "warn"
            assert status_1["connect"] is None
            assert status_1["connect_unavailable"] == BOT_CONFLICT_WITH_OWNER
            # Steady conflict re-checks POST /telegram/feed/poller without repeating Telegram getMe.
            assert await plugin._RUNTIME._ready_client() is None
            assert [m for m, _ in tg_1.calls] == ["getMe"]
            assert len(host_1.poller_claims) == 2
        finally:
            plugin._RUNTIME._run_task = None
            api_1.unload_cb()

        # Case 2: 409 account_changed and unavailable/unknown POST outcomes -> NO provider work,
        # NO observational fallback to GET /telegram/feed/identity (even when identity would say False!),
        # and clean recovery once POST returns explicit owned: True.
        poller_answers: List[Any] = [
            PresenceHostHTTPError(409, "account changed", {"ok": False, "code": "account_changed"}),
            PresenceHostHTTPError(503, "settings unknown", {"ok": False, "code": "settings_unknown"}),
            PresenceHostHTTPError(403, "auth unavailable", {"ok": False, "relay_refusal": "auth_unavailable"}),
            ConnectionError("loopback down"),
            {"ok": True, "owned": "yes"},   # non-bool owned -> unknown, never proceed
            {"ok": False, "owned": True},   # ok != True -> unknown, never proceed
            {"ok": True, "owned": True},    # explicit owned: True -> proceed to deleteWebhook
            {"ok": True, "owned": True},    # explicit owned: True before getUpdates #1
            PresenceHostHTTPError(409, "account changed", {"ok": False, "code": "account_changed"}),  # before poll #2
        ]
        host_2 = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "telegram_group_feed": 1,
                "telegram_group_poll_claim": 1,
            },
            feed_identity=_v2_identity("1001", enabled=False, reason="feed_off", same_as_owner_bridge=False, include_identity=False),
            poller_handler=lambda _p, _h: poller_answers.pop(0),
        )
        tg_2 = _FakeTelegramTransport(bot_id=2002, username="b_bot")
        sub_2 = PresenceHostClient(state_dir=tmp_path / "c2", token_provider=lambda: "tok-2", host_base="http://127.0.0.1:1", transport=host_2)
        await sub_2.discover_delivery_support()
        assert sub_2.telegram_group_poll_claim == 1
        feed_2 = OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "tok-2", transport=host_2)
        rt_2 = TelegramTransportRuntime(
            state_dir=tmp_path / "c2",
            token_provider=lambda: "2002:b-secret",
            logger=None,
            submitter=sub_2,
            client_factory=lambda t: TelegramClient(t, transport=tg_2),
            owner_feed=feed_2,
        )
        try:
            # First 6 non-owned outcomes (409, 503, 403, network error, invalid 200 shapes) -> NO deleteWebhook, NO fallback!
            for _ in range(6):
                assert await rt_2._ready_client() is None
                assert rt_2._runtime_state != "conflict_with_owner_bot"
            assert not any(m in ("deleteWebhook", "getUpdates") for m, _ in tg_2.calls)
            assert host_2.identity_queries == []

            # 7th answer is explicit owned: True -> deleteWebhook runs and client initializes.
            client = await rt_2._ready_client()
            assert client is not None
            assert [m for m, _ in tg_2.calls if m == "deleteWebhook"] == ["deleteWebhook"]
            assert host_2.identity_queries == []

            # 8th answer owned: True -> clear for getUpdates; 9th answer 409 account_changed -> invalidates client & waits.
            assert await rt_2._claim_b_poll_ownership("2002", token="2002:b-secret") == "clear"
            assert await rt_2._claim_b_poll_ownership("2002", token="2002:b-secret") == "wait"
            assert rt_2._client is None and rt_2._runtime_state == "not_started"
            assert host_2.identity_queries == []
        finally:
            rt_2.store.close()

        # Case 3: Mid-poll conflict (owned=False before next getUpdates) transitions to typed conflict and stops polling.
        mid_answers: List[Any] = [
            {"ok": True, "owned": True},   # before deleteWebhook
            {"ok": True, "owned": True},   # before getUpdates #1
            {"ok": True, "owned": False, "holder": "telegram"},  # before getUpdates #2 -> typed conflict!
        ]
        host_3 = _ScriptedHostTransport(
            identity_caps={
                "presence_delivery_version": 1,
                "places_version": 1,
                "telegram_group_feed": 1,
                "telegram_group_poll_claim": 1,
            },
            poller_handler=lambda _p, _h: mid_answers.pop(0) if mid_answers else {"ok": True, "owned": False, "holder": "telegram"},
        )
        tg_3 = _FakeTelegramTransport(bot_id=2002, username="b_bot")
        sub_3 = PresenceHostClient(state_dir=tmp_path / "c3", token_provider=lambda: "tok-3", host_base="http://127.0.0.1:1", transport=host_3)
        feed_3 = OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "tok-3", transport=host_3)
        rt_3 = TelegramTransportRuntime(
            state_dir=tmp_path / "c3",
            token_provider=lambda: "2002:b-secret",
            logger=None,
            submitter=sub_3,
            client_factory=lambda t: TelegramClient(t, transport=tg_3),
            owner_feed=feed_3,
        )
        task_3 = asyncio.create_task(rt_3.run())
        try:
            await _wait_until(lambda: rt_3._runtime_state == "conflict_with_owner_bot")
            assert [m for m, _ in tg_3.calls] == ["getMe", "deleteWebhook", "getUpdates"]
            assert rt_3.confirmed_bot() == ("", BOT_CONFLICT_WITH_OWNER)
            assert host_3.identity_queries == []
        finally:
            rt_3.stop()
            task_3.cancel()
            await asyncio.gather(task_3, return_exceptions=True)

        # Case 4: JSON boolean True for telegram_group_poll_claim is NOT accepted as integer 1.
        host_bool = _ScriptedHostTransport(
            identity_caps={"presence_delivery_version": 1, "places_version": 1, "telegram_group_poll_claim": True},
        )
        sub_bool = PresenceHostClient(state_dir=tmp_path / "c4", token_provider=lambda: "tok-4", host_base="http://127.0.0.1:1", transport=host_bool)
        await sub_bool.discover_delivery_support()
        assert sub_bool.telegram_group_poll_claim == 0

    asyncio.run(run_case())


def test_real_core_manifest_validation_and_place_guard_a_vs_b(tmp_path):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; real core manifest & place_guard checks skipped")

    script = r"""
import json, os, pathlib, shutil, sys
from types import SimpleNamespace

core_root, skill_root, root = map(pathlib.Path, sys.argv[1:4])
sys.path.insert(0, str(core_root))

os.environ["OUROBOROS_RUNTIME_MODE"] = "advanced"
os.environ["OUROBOROS_HOST_SERVICE_PORT"] = "1"
data, skills = root / "data", root / "skills"
data.mkdir(parents=True, exist_ok=True)
skills.mkdir(parents=True, exist_ok=True)
pathlib.Path(os.environ["OUROBOROS_SETTINGS_PATH"]).write_text(
    json.dumps({"TELEGRAM_PUBLIC_BOT_TOKEN": "2002:fixture-token"}), encoding="utf-8"
)
shutil.copytree(
    skill_root,
    skills / "telegram-bot",
    ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".DS_Store"),
)

from ouroboros import extension_loader, place_guard, place_projection, place_reply, places, safety, tool_effects
from ouroboros.config import load_settings
from ouroboros.extension_surface_names import extension_surface_name
from ouroboros.extension_ui_validation import validate_contributions
from ouroboros.skill_loader import SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants

safety.check_safety = lambda *_a, **_kw: (True, "")
loaded = find_skill(data, "telegram-bot", repo_path=str(skills))
assert loaded is not None and not loaded.load_error, getattr(loaded, "load_error", "missing")

# 1. Exact old/new core contribution validation: 1 legacy Place B, zero unknown fields/warnings.
clean, warnings = validate_contributions(loaded.manifest)
assert len(clean) == 1 and clean[0]["id"] == "place" and not warnings, (clean, warnings)

save_enabled(data, "telegram-bot", True)
save_review_state(data, "telegram-bot", SkillReviewState(status="pass", content_hash=loaded.content_hash))
save_skill_grants(
    data,
    "telegram-bot",
    ["TELEGRAM_PUBLIC_BOT_TOKEN"],
    content_hash=loaded.content_hash,
    requested_keys=["TELEGRAM_PUBLIC_BOT_TOKEN"],
    granted_permissions=["presence"],
    requested_permissions=["presence"],
)
errors = {k: str(v) for k, v in extension_loader.reload_all(data, load_settings, repo_path=str(skills)).items() if v}
assert not errors, errors

# 2. place_projection.connect_declared_place succeeds on this manifest (rejects >1 place declarations).
connected_b = place_projection.connect_declared_place(
    data,
    "telegram-bot",
    account_id="2002",
    conversation_id="-100500",
    label="B Group",
)
assert connected_b.account_id == "2002" and connected_b.conversation_id == "-100500"

# 3. Real core place_guard enforces exact (account_id, chat_id) separation when A and B share group -100500.
place_a = places.connect_place(
    data,
    connector="telegram-bot",
    provider="telegram",
    account_id="1001",
    conversation_id="-100500",
    label="Telegram · A Group",
    scope="conversation",
    contribution_id="owner_bot",
    wakes=["any_message"],
)
places.edit_place(data, connected_b.place_id, "set_state", author="owner", state="active")
places.edit_place(data, place_a.place_id, "set_state", author="owner", state="silent")

send_surface = extension_surface_name("telegram-bot", "telegram_send")
react_surface = extension_surface_name("telegram-bot", "telegram_react")
send_tool = extension_loader.get_tool(send_surface)
react_tool = extension_loader.get_tool(react_surface)
send_eff = tool_effects.effect_of(send_surface, ext_tool=send_tool)
react_eff = tool_effects.effect_of(react_surface, ext_tool=react_tool)
assert "account_id" in send_eff.targets and "chat_id" in send_eff.targets
assert "account_id" in react_eff.targets and "chat_id" in react_eff.targets

ctx = SimpleNamespace(drive_root=data, task_id="caller", task_metadata={})
refuse_b = place_guard.dispatch_refusal(
    ctx, send_surface, {"account_id": "2002", "chat_id": "-100500", "kind": "message", "text": "ok"}, ext_tool=send_tool,
)
refuse_a_silent = place_guard.dispatch_refusal(
    ctx, send_surface, {"account_id": "1001", "chat_id": "-100500", "kind": "message", "text": "no"}, ext_tool=send_tool,
)

# Flip: make A active and B silent -> now A allowed, B refused!
places.edit_place(data, place_a.place_id, "set_state", author="owner", state="active")
places.edit_place(data, connected_b.place_id, "set_state", author="owner", state="silent")
refuse_a_active = place_guard.dispatch_refusal(
    ctx, send_surface, {"account_id": "1001", "chat_id": "-100500", "kind": "message", "text": "yes"}, ext_tool=send_tool,
)
refuse_b_silent = place_guard.dispatch_refusal(
    ctx, send_surface, {"account_id": "2002", "chat_id": "-100500", "kind": "message", "text": "no"}, ext_tool=send_tool,
)

# 4. place_reply.speak_line prefills account_id for A turns when target includes account_id.
speak = place_reply.speak_line({
    "wake_cause": "focus",
    "presence": {
        "transport_skill": "telegram-bot",
        "place": {"label": "A Group", "state": "active"},
        "event": {"account_id": "1001", "conversation_id": "-100500", "thread_id": "", "message": {"message_id": 7}},
    },
})

print(json.dumps({
    "refuse_b": refuse_b,
    "refuse_a_silent": refuse_a_silent,
    "refuse_a_active": refuse_a_active,
    "refuse_b_silent": refuse_b_silent,
    "speak": speak,
}))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"),
        "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"),
        "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run(
        [sys.executable, "-c", script, str(pathlib.Path(core).resolve()), str(SKILL_ROOT), str(tmp_path)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stdout + "\n" + run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["refuse_b"] == ""
    assert out["refuse_a_silent"] != ""
    assert out["refuse_a_active"] == ""
    assert out["refuse_b_silent"] != ""
    assert "account_id=1001" in out["speak"] and "chat_id=-100500" in out["speak"]
