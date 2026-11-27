"""Contracts of the owner's own bot beside the standalone bot: order, outcomes, waits, turns and the card."""

from __future__ import annotations

import asyncio
import io
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from telegram_bot.api import TelegramApiError, TelegramClient, TelegramRequestNotSent, _NoUploadRedirect
from telegram_bot.custody import OWNER_BOT_CHANGED, CustodyStore
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostClient, PresenceHostError, UrllibPresenceHostTransport
from telegram_bot.owner_feed import (
    RELAY_API_SERVER,
    SOURCE_OWNER_BOT,
    OwnerFeedClient,
    RelayDownloadTimeout,
    RelayRefusalError,
    RelayTelegramTransport,
    owner_state_dir,
    owner_store_path,
)
from telegram_bot.runtime import TelegramTransportRuntime
from telegram_bot.status_summary import card_summary, summarize, summarize_owner
from telegram_bot.tools import SendRefused, resolve_tool_source, turn_place_or_bind, turn_target


def _binding(account: str = "1001", revision: int = 1, place: str = "p", thread: str = "") -> dict:
    return {"version": 1, "connector": "telegram-bot", "provider": "telegram", "account_id": account,
            "conversation_id": "-100500", "thread_id": thread, "place_id": place, "connected_revision": revision}


def _row(chat: str = "-100500", topic=None, account=None, binding=None) -> dict:
    payload = {"kind": "message", "chat_id": chat, "text": "hi", "_rendered_chunks": [{"text": "hi", "parse_mode": ""}]}
    if topic is not None:
        payload["topic_id"] = topic
    if account is not None:
        payload["account_id"] = account
    if binding is not None:
        payload["_place_binding"] = binding
    return payload


def _group_update(update_id: int, message_id: int = 7, **extra) -> dict:
    return {"update_id": update_id, "message": {
        "message_id": message_id, "date": 1730000000, "text": "hello",
        "from": {"id": 55, "is_bot": False, "first_name": "Bob"},
        "chat": {"id": -100500, "type": "supergroup", "title": "Team"}, **extra}}


def test_standalone_rows_keep_one_order_whether_or_not_the_caller_named_the_bot(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    assert store.owner_bot is False
    store.enqueue_outbox("a-unnamed", _row(topic=5))  # an earlier version kept the topic as a number
    store.enqueue_outbox("b-named", _row(topic="5", account="2002"))
    store.enqueue_outbox("c-other-topic", _row(topic="6", account="2002"))
    assert store.claim_outbox().delivery_id == "a-unnamed"
    # The named row waits behind the unnamed one of its chat and topic; another topic does not.
    assert store.claim_outbox().delivery_id == "c-other-topic"
    assert store.claim_outbox() is None
    store.mark_delivered("a-unnamed", provider_receipt={})
    assert store.claim_outbox().delivery_id == "b-named"

    reverse = CustodyStore(tmp_path / "reverse" / "custody.sqlite3")
    reverse.enqueue_outbox("named-first", _row(account="2002"))
    reverse.enqueue_outbox("unnamed-later", _row())
    assert reverse.claim_outbox().delivery_id == "named-first"
    assert reverse.claim_outbox() is None


def test_owner_rows_wait_only_behind_their_own_bot_and_connection(tmp_path):
    store = CustodyStore(owner_store_path(tmp_path))
    assert store.owner_bot is True
    store.enqueue_outbox("a1-rev1", _row(account="1001", binding=_binding("1001", 1)))
    store.enqueue_outbox("a1-rev1-next", _row(account="1001", binding=_binding("1001", 1)))
    store.enqueue_outbox("a1-rev2", _row(account="1001", binding=_binding("1001", 2)))
    store.enqueue_outbox("a2-rev1", _row(account="1002", binding=_binding("1002", 1)))
    assert [store.claim_outbox().delivery_id for _ in range(3)] == ["a1-rev1", "a1-rev2", "a2-rev1"]
    assert store.claim_outbox() is None  # only the next row of the same bot and connection waits


def _bound_relay(tmp_path) -> RelayTelegramTransport:
    return RelayTelegramTransport(account_id="1001", host_base="http://127.0.0.1:1", token_provider=lambda: "tok",
                                  allowed_files_root=tmp_path, binding=_binding(),
                                  host_transport=UrllibPresenceHostTransport())


def _send(relay: RelayTelegramTransport):
    return asyncio.run(relay.post_json(f"{RELAY_API_SERVER}/botrelay/sendMessage",
                                       {"chat_id": "-100500", "text": "hi"}, 5.0))


@pytest.mark.parametrize("reason, unsent", [
    (ConnectionRefusedError(), True),
    (socket.gaierror(), True),
    (TimeoutError("timed out"), False),
    (BrokenPipeError(), False),
])
def test_the_production_relay_client_tells_an_unsent_request_from_a_lost_answer(tmp_path, monkeypatch, reason, unsent):
    def fail(_opener, _request, data=None, timeout=None):
        raise urllib.error.URLError(reason)

    # The production Host transport opens every request through a no-redirect opener (host._open_host).
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail)
    with pytest.raises(TelegramApiError) as raised:
        _send(_bound_relay(tmp_path))
    assert isinstance(raised.value, TelegramRequestNotSent) is unsent
    assert raised.value.error_code == 0


@pytest.mark.parametrize("status, body, kind", [
    (503, {"relay_outcome": "not_sent"}, "not_sent"),
    (502, {"relay_outcome": "unknown"}, "unknown"),
    (403, {"ok": False, "error_code": 403, "description": "Groups are off.", "relay_refusal": "feed_off"}, "refusal"),
])
def test_the_production_relay_client_reads_the_relays_typed_answers(tmp_path, monkeypatch, status, body, kind):
    def answer(_opener, request, data=None, timeout=None):
        raise urllib.error.HTTPError(request.full_url, status, "relay", {}, io.BytesIO(json.dumps(body).encode()))

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", answer)
    with pytest.raises(TelegramApiError) as raised:
        _send(_bound_relay(tmp_path))
    if kind == "refusal":
        assert isinstance(raised.value, RelayRefusalError) and raised.value.relay_refusal == "feed_off"
    else:
        assert isinstance(raised.value, TelegramRequestNotSent) is (kind == "not_sent")
        assert raised.value.error_code == 0


class _CutAnswer(io.BytesIO):
    """An answer whose body is shorter than the length it declared."""

    def __init__(self, body: bytes, declared: int):
        super().__init__(body)
        self.headers = {"Content-Length": str(declared)}


def test_the_host_transport_follows_no_redirect_and_reads_a_cut_answer_as_unknown(tmp_path, monkeypatch):
    handlers = []
    whole = json.dumps({"ok": True, "result": {"message_id": 7, "chat": {"id": -100500, "type": "supergroup"}}})

    class _Opener:
        def open(self, request, timeout=None):
            return _CutAnswer(whole.encode()[:-5], len(whole))

    def build(*given):
        handlers.extend(given)
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(TelegramApiError) as raised:
        _send(_bound_relay(tmp_path))
    # A cut answer says nothing about what Telegram did: unknown, never "not sent" and never a success.
    assert not isinstance(raised.value, TelegramRequestNotSent) and raised.value.error_code == 0
    assert any(isinstance(handler, _NoUploadRedirect) for handler in handlers)


class _Telegram:
    def __init__(self):
        self.calls = []

    async def post_json(self, url, payload, timeout_sec):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method == "getMe":
            return {"ok": True, "result": {"id": 2002, "is_bot": True, "username": "b_bot"}}
        return {"ok": True, "result": True}


class _Host:
    def __init__(self):
        self.answer = None  # None: Ouroboros does not answer

    async def request_json(self, method, url, *, headers, payload, timeout_sec):
        if self.answer is None:
            raise PresenceHostError("presence Host request failed: URLError")
        if url.endswith("/identity"):
            return dict(self.answer)
        raise AssertionError(f"unexpected Host request {method} {url}")


def test_the_standalone_bot_waits_for_an_answer_from_ouroboros_before_contacting_telegram(tmp_path):
    async def case():
        host, telegram = _Host(), _Telegram()
        runtime = TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: "2002:b-secret", logger=None,
            submitter=PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "tok",
                                         host_base="http://127.0.0.1:1", transport=host),
            client_factory=lambda token: TelegramClient(token, transport=telegram),
            owner_feed=OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "tok", transport=host),
        )
        runtime.store.enqueue_outbox("telegram-send:r1", _row())
        # No answer is not an older Ouroboros: Telegram is not contacted, and the queued row waits using no attempt.
        assert await runtime._ready_client() is None
        assert await runtime.process_one_outbox() is True
        assert telegram.calls == []
        receipt = runtime.store.delivery_receipt("telegram-send:r1")
        assert receipt["state"] == "held" and receipt["attempts"] == 0
        assert "Waiting for Ouroboros" in receipt["error"] and "token" not in receipt["error"]
        # An Ouroboros that answers without the group feed keeps the 0.13.1 path.
        host.answer = {"presence_delivery_version": 1, "places_version": 1}
        assert await runtime._ready_client() is not None
        assert telegram.calls == ["deleteWebhook", "getMe"]
        runtime.store.close()

    asyncio.run(case())


def test_saved_connector_token_held_by_ouroboros_keeps_delivery_waiting(tmp_path):
    async def case():
        host, telegram = _Host(), _Telegram()
        host.answer = {"v": 1, "places_version": 1, "telegram_group_feed": 1,
                       "connector_token_held": True, "enabled": False, "reason": "feed_off"}
        feed = OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "tok", transport=host)
        runtime = TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: "", logger=None,
            submitter=PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "tok",
                                         host_base="http://127.0.0.1:1", transport=host),
            client_factory=lambda token: TelegramClient(token, transport=telegram), owner_feed=feed,
        )
        runtime._run_task = asyncio.current_task()
        try:
            await feed.identity()
            runtime.store.enqueue_outbox("telegram-send:held", _row())
            assert await runtime.process_one_outbox()
            receipt = runtime.store.delivery_receipt("telegram-send:held")
            assert receipt["state"] == "held" and receipt["attempts"] == 0
            assert telegram.calls == []
            snapshot = summarize(runtime.status_snapshot())
            assert snapshot["text"].startswith("Same bot as personal chat")
            assert "missing" not in snapshot["text"].lower()
        finally:
            runtime.store.close()

    asyncio.run(case())


def test_ordinary_owner_file_relay_allows_native_time_to_answer(tmp_path):
    async def case():
        observed = {}

        class Host:
            async def request_json(self, method, url, *, headers, payload, timeout_sec):
                observed[payload["method"]] = timeout_sec
                if payload["method"] == "getFile":
                    result = {"file_path": "documents/sample.txt", "file_size": 4}
                else:
                    result = {"message_id": 9, "chat": {"id": -100500, "type": "supergroup"}}
                return {"ok": True, "result": result}

        def receive(url, headers, timeout_sec, max_bytes):
            observed["file"] = timeout_sec
            return b"test"

        relay = RelayTelegramTransport(account_id="1001", host_base="http://127.0.0.1:1", token_provider=lambda: "tok",
                                       allowed_files_root=tmp_path, binding=_binding(), host_transport=Host(),
                                       bytes_transport=receive)
        client = TelegramClient("relay", api_server=RELAY_API_SERVER, transport=relay)
        file = await client.download_file("sample", tmp_path / "download.txt")
        assert file.read_bytes() == b"test"
        assert (await relay.post_multipart(f"{RELAY_API_SERVER}/botrelay/sendDocument", {"chat_id": "-100500"},
                                           "document", file, 60))["ok"]
        assert observed == {"getFile": 50.0, "file": 90.0, "sendDocument": 150.0}

    asyncio.run(case())


@pytest.mark.parametrize("failure", [
    "file_wait", "connect_timeout", "host_disconnect", "short_body", "unknown_502", "rate_limit",
    "permissions", "metadata_wait", "native_file_wait", "untyped_504", "incomplete_504",
])
def test_only_the_relay_file_wait_after_getfile_counts_as_a_download_timeout(tmp_path, monkeypatch, failure):
    state = {"failure": failure, "metadata": False}

    class Host:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            assert payload["method"] == "getFile"
            state["metadata"] = state["failure"] != "metadata_wait"
            if not state["metadata"]:
                raise TimeoutError()
            return {"ok": True, "result": {"file_path": "documents/sample.txt", "file_size": 4}}

    def read_file(request, timeout_sec):
        assert state["metadata"] and timeout_sec == 90.0
        mode = state["failure"]
        if mode == "file_wait":
            raise TimeoutError()
        if mode == "connect_timeout":
            raise urllib.error.URLError(TimeoutError())
        if mode == "host_disconnect":
            raise ConnectionResetError()
        if mode == "short_body":
            return _CutAnswer(b"te", 4)
        errors = {
            "unknown_502": (502, {"relay_outcome": "unknown"}),
            "native_file_wait": (504, {"relay_outcome": "file_read_timeout"}),
            "untyped_504": (504, {"description": "Gateway Timeout"}),
            "incomplete_504": (504, {"relay_outcome": "file_read_timeout"}),
            "rate_limit": (429, {"ok": False, "error_code": 429, "description": "Too Many Requests"}),
            "permissions": (403, {"relay_refusal": "auth_unavailable", "error_code": 403}),
        }
        if mode in errors:
            status, body = errors[mode]
            raw = json.dumps(body).encode()
            headers = {"Content-Length": str(len(raw) + 1)} if mode == "incomplete_504" else {}
            raise urllib.error.HTTPError(request.full_url, status, "relay", headers, io.BytesIO(raw))
        assert mode == "success"
        return _CutAnswer(b"test", 4)

    monkeypatch.setattr("telegram_bot.owner_feed._open_host", read_file)
    relay = RelayTelegramTransport(account_id="1001", host_base="http://127.0.0.1:1", token_provider=lambda: "tok",
                                   allowed_files_root=tmp_path, binding=_binding(), host_transport=Host())
    client = TelegramClient("relay", api_server=RELAY_API_SERVER, transport=relay)
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=None,
                                      submitter=SimpleNamespace(), source_mode=SOURCE_OWNER_BOT)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr("telegram_bot.runtime.time", SimpleNamespace(time=lambda: clock.now))
    destination = tmp_path / "download.txt"
    counted = failure in {"file_wait", "native_file_wait"}
    try:
        for attempts, elapsed in enumerate((0, 1, 2, 3, 4, 1799, 1800), start=1):
            clock.now = 1000.0 + elapsed
            with pytest.raises(TelegramApiError) as raised:
                asyncio.run(client.download_file("sample", destination))
            assert isinstance(raised.value, RelayDownloadTimeout) is counted
            note = runtime._bounded_failure("event", 0, raised.value)
            assert runtime.store.download_failure("event", 0)["own_failures"] == (attempts if counted else 0)
            if not counted or elapsed < 1800:
                assert note is None
            else:
                assert note == "not received after 7 attempts over 30 min; last: timed out waiting for file bytes (cause unknown)"
            assert not destination.exists()
        state["failure"] = "success"
        assert asyncio.run(client.download_file("sample", destination)).read_bytes() == b"test"
    finally:
        runtime.store.close()


def test_an_owner_bot_turn_is_known_by_its_own_event_and_never_moved_to_another_bot(tmp_path):
    root = tmp_path / "state"
    event = parse_telegram_update(_group_update(77), bot_account_id="1001", places=True, group_only=True)
    CustodyStore(owner_store_path(root)).commit_update(
        3, event, advance_offset=False, feed_cursor=("1001", 4), place_binding=_binding("1001"))
    ctx = SimpleNamespace(task_id="t", task_metadata={"presence": {"event": {
        "provider": "telegram", "account_id": "1001", "conversation_id": "-100500",
        "source_event_id": event.source_event_id}}})

    def resolve(context, **kwargs):
        return resolve_tool_source(root, ctx=context, standalone_account=lambda: "2002", owner_account=lambda: "1002",
                                   standalone_configured=lambda: True, **kwargs)

    with pytest.raises(SendRefused) as omitted:
        resolve(ctx)
    assert omitted.value.code == "account_id_required" and "account_id='1001'" in str(omitted.value)
    with pytest.raises(SendRefused) as named:
        resolve(ctx, account_id="1001")
    assert named.value.code == "account_not_active" and "1002" not in str(named.value)
    # A standalone bot's turn, also before getMe confirmed that bot again, keeps the 0.13.1 path.
    b_turn = SimpleNamespace(task_id="t2", task_metadata={"presence": {"event": {
        "provider": "telegram", "account_id": "2002", "conversation_id": "-100500",
        "source_event_id": "telegram:2002:12"}}})
    source = resolve_tool_source(root, ctx=b_turn, standalone_account=lambda: "", owner_account=lambda: "1002",
                                 standalone_configured=lambda: True)
    assert source.source == "standalone" and source.explicit is False


def _owner_runtime(tmp_path) -> TelegramTransportRuntime:
    return TelegramTransportRuntime(state_dir=owner_state_dir(tmp_path), settings_dir=tmp_path,
                                    token_provider=lambda: "", logger=None,
                                    submitter=SimpleNamespace(places_version=1), source_mode=SOURCE_OWNER_BOT)


def test_two_owner_bots_events_with_the_same_update_number_are_both_kept(tmp_path):
    runtime = _owner_runtime(tmp_path)
    for feed_id, account in ((1, "1001"), (2, "1002")):
        runtime._commit_feed_item(
            {"feed_id": feed_id, "update": _group_update(500),
             "place": {"place_id": f"p-{account}", "connected_revision": 1, "thread_id": ""}},
            account=account, username="", download_limit=20 * 1024 * 1024)
    with runtime.store._connect() as conn:
        rows = [tuple(row) for row in conn.execute("SELECT event_id, update_id FROM inbox ORDER BY update_id")]
    assert rows == [("telegram:1001:500", 1), ("telegram:1002:500", 2)]
    assert runtime.store.owner_feed_offset("1002") == 3
    runtime.store.close()


def test_a_busy_owner_feed_reads_complete_pages_and_records_them_in_order(tmp_path, monkeypatch):
    # Ordinary Telegram messages have Unicode and can quote earlier Unicode messages. A full page of these
    # valid events exceeds the production Host transport's response budget; a smaller page fits.
    text = "📚" * 3800
    items = [
        {"feed_id": i, "update": _group_update(i, message_id=i + 100, text=text, reply_to_message={
            "message_id": i, "text": text, "from": {"id": 56, "first_name": "Alice"},
        }), "place": {"place_id": "p-1001", "connected_revision": 1, "thread_id": ""}}
        for i in range(1, 101)
    ]
    requests = []

    def open_feed(_opener, request, data=None, timeout=None):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        offset, limit = int(query["offset"][0]), int(query.get("limit", ["100"])[0])
        requests.append((offset, limit))
        page = [item for item in items if item["feed_id"] >= offset][:limit]
        body = json.dumps({"ok": True, "result": page}, ensure_ascii=False).encode()
        return _CutAnswer(body, len(body))

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_feed)
    runtime = _owner_runtime(tmp_path)
    feed = OwnerFeedClient(host_base="http://127.0.0.1:1", token_provider=lambda: "synthetic")

    async def drain():
        for expected_offset in (51, 101):
            offset = runtime.store.owner_feed_offset("1001")
            page = await feed.updates("1001", offset=offset)
            assert runtime.store.owner_feed_offset("1001") == offset
            for item in page:
                runtime._commit_feed_item(item, account="1001", username="", download_limit=20 * 1024 * 1024)
            assert runtime.store.owner_feed_offset("1001") == expected_offset

    asyncio.run(drain())
    assert requests == [(0, 100), (0, 50), (51, 100)]
    with runtime.store._connect() as conn:
        assert [row[0] for row in conn.execute("SELECT update_id FROM inbox ORDER BY update_id")] == list(range(1, 101))
    runtime.store.close()


@pytest.mark.parametrize("counted", [False, True], ids=["individual", "count"])
def test_owner_feed_reaction_keeps_the_topic_resolved_by_its_place(tmp_path, counted):
    runtime = _owner_runtime(tmp_path)
    chat = {"id": -100500, "type": "supergroup", "title": "Team", "is_forum": True}
    emoji = {"type": "emoji", "emoji": "👍"}
    body = {"chat": chat, "message_id": 30, "date": 1730000000}
    if counted:
        kind = "message_reaction_count"
        body["reactions"] = [{"type": emoji, "total_count": 2}]
    else:
        kind = "message_reaction"
        body.update(user={"id": 55, "is_bot": False, "first_name": "Bob"}, old_reaction=[], new_reaction=[emoji])
    runtime._commit_feed_item(
        {"feed_id": 7, "update": {"update_id": 500, kind: body},
         "place": {"place_id": "topic-13", "connected_revision": 3, "thread_id": "13"}},
        account="1001", username="", download_limit=20 * 1024 * 1024,
    )
    lease = runtime.store.claim_inbox()
    assert lease.payload["thread_id"] == "13"
    assert lease.payload["conversation"]["topic_id"] == 13
    assert lease.payload["conversation_key"] == "telegram:1001:-100500:13"
    assert lease.payload["message"]["event_kind"] == kind
    assert lease.payload["message"]["reaction"]["target"] == {"message_id": "30", "resolution": "resolved"}
    assert lease.place_binding == _binding(place="topic-13", revision=3, thread="13")
    assert runtime.store.owner_feed_offset("1001") == 8
    runtime.store.close()


class _EndedRelay:
    def with_binding(self, _binding):
        return self

    async def post_json(self, url, payload, timeout_sec):
        raise RelayRefusalError("getFile", "This group is no longer connected as it was.",
                                relay_refusal="binding_changed")


def test_an_attachment_whose_connection_ended_is_settled_instead_of_looping(tmp_path):
    async def case():
        runtime = _owner_runtime(tmp_path)
        runtime._client = TelegramClient("relay-account-1001", transport=_EndedRelay(), api_server=RELAY_API_SERVER)
        runtime._bot_id, runtime._feed_enabled = "1001", True
        photo = [{"file_id": "f1", "file_unique_id": "u1", "width": 1, "height": 1, "file_size": 10}]
        runtime._commit_feed_item(
            {"feed_id": 4, "update": _group_update(501, photo=photo, caption="look"),
             "place": {"place_id": "p", "connected_revision": 1, "thread_id": ""}},
            account="1001", username="", download_limit=20 * 1024 * 1024)
        lease = runtime.store.claim_inbox()
        assert await runtime._stage_attachments(lease, place_binding=lease.place_binding) == []
        [attachment] = lease.payload["message"]["attachments"]
        assert attachment["status"] == "failed" and "binding_changed" in attachment["note"]
        runtime.store.close()

    asyncio.run(case())


def test_native_file_timeout_settles_only_that_attachment_after_the_existing_budget(tmp_path, monkeypatch):
    async def case():
        clock = SimpleNamespace(now=1000.0)
        monkeypatch.setattr("telegram_bot.runtime.time", SimpleNamespace(time=lambda: clock.now))
        runtime = _owner_runtime(tmp_path)
        ids = []

        class Host:
            async def request_json(self, method, url, *, headers, payload, timeout_sec):
                assert payload["method"] == "getFile"
                ids.append(payload["params"]["file_id"])
                return {"ok": True, "result": {"file_path": "photos/synthetic.jpg", "file_size": 4}}

        def read_file(request, timeout_sec):
            body = io.BytesIO(b'{"relay_outcome":"file_read_timeout"}')
            raise urllib.error.HTTPError(request.full_url, 504, "relay", {}, body)

        monkeypatch.setattr("telegram_bot.owner_feed._open_host", read_file)
        relay = RelayTelegramTransport(account_id="1001", host_base="http://127.0.0.1:1", token_provider=lambda: "tok",
                                       allowed_files_root=tmp_path, binding=_binding(), host_transport=Host())
        runtime._client = TelegramClient("relay", transport=relay, api_server=RELAY_API_SERVER)
        runtime._bot_id, runtime._feed_enabled = "1001", True
        photo = [{"file_id": "synthetic-photo", "file_unique_id": "u1", "width": 1, "height": 1, "file_size": 4}]
        runtime._commit_feed_item(
            {"feed_id": 4, "update": _group_update(501, photo=photo, caption="look"),
             "place": {"place_id": "p", "connected_revision": 1, "thread_id": ""}},
            account="1001", username="", download_limit=20 * 1024 * 1024)
        lease = runtime.store.claim_inbox()
        try:
            for elapsed in (0, 1, 2, 3, 4, 1799):
                clock.now = 1000.0 + elapsed
                with pytest.raises(RelayDownloadTimeout):
                    await runtime._stage_attachments(lease, place_binding=lease.place_binding)
                assert lease.payload["message"]["attachments"][0].get("status") != "failed"
            clock.now = 2800.0
            assert await runtime._stage_attachments(lease, place_binding=lease.place_binding) == []
            [attachment] = lease.payload["message"]["attachments"]
            assert ids == ["synthetic-photo"] * 7 and attachment["file_id"] == "synthetic-photo"
            assert attachment["status"] == "failed" and "cause unknown" in attachment["note"]
            assert runtime.store.download_failure(lease.event_id, 0) is None
        finally:
            runtime.store.close()

    asyncio.run(case())


class _NoTelegram:
    """A relay that is never to be asked anything."""

    def __init__(self):
        self.calls = []

    def with_binding(self, _binding):
        return self

    async def post_json(self, url, payload, timeout_sec):
        self.calls.append(url.rsplit("/", 1)[-1])
        raise AssertionError("nothing is asked of Telegram here")


class _Submitter:
    places_version = 1

    def __init__(self):
        self.submitted = []

    async def submit(self, event, staged, *, binding_id="", connected_revision=None):
        self.submitted.append((event["source_event_id"], binding_id, connected_revision))
        return SimpleNamespace(binding_id=binding_id, turn_ref="turn-1", outcome="message", text="ok", work_ref="",
                               delivery_reporting_version=1, continuation_ref="", output_ref="", reply_anchor=None)


_PHOTO = [{"file_id": "f1", "file_unique_id": "u1", "width": 1, "height": 1, "file_size": 10}]


def _feed_item(runtime, feed_id: int, account: str, update: dict) -> None:
    runtime._commit_feed_item({"feed_id": feed_id, "update": update,
                               "place": {"place_id": f"p-{account}", "connected_revision": 1, "thread_id": ""}},
                              account=account, username="", download_limit=20 * 1024 * 1024)


def _inbox_row(runtime, event_id: str) -> tuple:
    with runtime.store._connect() as conn:
        return tuple(conn.execute("SELECT state, last_error FROM inbox WHERE event_id=?", (event_id,)).fetchone())


def test_an_update_of_an_earlier_owner_bot_ends_before_any_download_and_never_holds_the_current_one(tmp_path):
    async def case():
        submitter = _Submitter()
        runtime = TelegramTransportRuntime(state_dir=owner_state_dir(tmp_path), settings_dir=tmp_path,
                                           token_provider=lambda: "", logger=None, submitter=submitter,
                                           source_mode=SOURCE_OWNER_BOT)
        relay = _NoTelegram()
        runtime._client = TelegramClient("relay-account-1002", transport=relay, api_server=RELAY_API_SERVER)
        runtime._bot_id, runtime._feed_enabled = "1002", True
        _feed_item(runtime, 4, "1001", _group_update(501, photo=_PHOTO, caption="look"))
        _feed_item(runtime, 5, "1002", _group_update(502, message_id=8))
        # The earlier bot's update ends with its own outcome: nothing is downloaded or passed on for it.
        assert await runtime.process_one_inbox() is True
        assert relay.calls == [] and submitter.submitted == []
        assert _inbox_row(runtime, "telegram:1001:501") == ("failed", OWNER_BOT_CHANGED)
        # The connected bot's update is passed on as before, under its own connection.
        assert await runtime.process_one_inbox() is True
        assert submitter.submitted == [("telegram:1002:502", "p-1002", 1)]
        reply = runtime.store.outbox_payload("presence:telegram:1002:502:turn")
        assert reply["account_id"] == "1002" and reply["_place_binding"]["place_id"] == "p-1002"
        assert await runtime.process_one_inbox() is False
        counts, card = runtime.store.read_status()
        # Left with the earlier bot is counted on its own, never as refused by Ouroboros.
        assert counts["inbox_retired"] == 1 and counts["inbox_failed"] == 0
        assert card["recent_problems"]["inbound"]["failed"] == 0
        runtime.store.close()

    asyncio.run(case())


def test_an_owner_bot_update_waits_while_no_bot_is_confirmed_for_groups(tmp_path):
    async def case():
        runtime = _owner_runtime(tmp_path)
        # Groups off or the feed not read: that is no other bot, so the update keeps waiting.
        runtime._bot_id, runtime._feed_enabled = "1002", False
        _feed_item(runtime, 4, "1001", _group_update(501, photo=_PHOTO, caption="look"))
        assert await runtime.process_one_inbox() is True
        state, error = _inbox_row(runtime, "telegram:1001:501")
        assert state == "pending" and error != OWNER_BOT_CHANGED
        runtime.store.close()

    asyncio.run(case())


def test_owner_inbox_text_is_delivered_once_when_its_confirmed_source_returns(tmp_path, monkeypatch):
    from telegram_bot import custody

    clock = [1_000.0]
    monkeypatch.setattr(custody.time, "time", lambda: clock[0])

    async def case():
        runtime = _owner_runtime(tmp_path)
        runtime.submitter = _Submitter()
        _feed_item(runtime, 4, "1001", _group_update(501))
        assert await runtime.process_one_inbox() is True
        assert _inbox_row(runtime, "telegram:1001:501") == ("pending", "Held: Owner bot group feed is not active.")
        assert runtime.submitter.submitted == []
        # The same connection returns. Its retained event is admitted once and the same bot answers.
        clock[0] += 6.0
        runtime._bot_id, runtime._feed_enabled = "1001", True
        assert await runtime.process_one_inbox() is True
        assert await runtime.process_one_inbox() is False
        assert runtime.submitter.submitted == [("telegram:1001:501", "p-1001", 1)]
        assert runtime.store.outbox_payload("presence:telegram:1001:501:turn")["account_id"] == "1001"
        runtime.store.close()

    asyncio.run(case())


def test_a_call_on_this_turns_own_event_keeps_the_connection_it_arrived_under(tmp_path):
    root = tmp_path / "state"
    event = parse_telegram_update(_group_update(78, message_id=9), bot_account_id="1001", places=True, group_only=True)
    saved = _binding("1001", revision=1, place="p-1")
    CustodyStore(owner_store_path(root)).commit_update(
        3, event, advance_offset=False, feed_cursor=("1001", 4), place_binding=saved)
    ctx = SimpleNamespace(task_id="t", task_metadata={"presence": {"event": {
        "provider": "telegram", "account_id": "1001", "conversation_id": "-100500",
        "source_event_id": event.source_event_id}}})
    binds = []

    def binder(account, chat, thread):
        binds.append((account, chat, thread))
        return {"allowed": True, "binding": {**_binding("1001", revision=2, place="p-1", thread=thread),
                                             "conversation_id": chat}}

    def place(chat, *, owner="1001", standalone="", **where):
        turn = turn_target(root, ctx, chat, **where)
        source = resolve_tool_source(root, account_id="1001", ctx=ctx, standalone_account=lambda: standalone,
                                     owner_account=lambda: owner, standalone_configured=lambda: bool(standalone),
                                     turn=turn)
        return turn_place_or_bind(source, turn, chat, topic_id=where.get("topic_id", ""), owner_place_binder=binder)

    # A reply in the turn's own chat and topic, and a reaction to its own message, keep the connection the event came
    # under: never bound again to a newer revision.
    assert place("-100500", topic_id="") == saved
    assert place("-100500", message_id=9) == saved
    assert binds == []
    # Another message or another chat is a new operation, bound now.
    assert place("-100500", message_id=10)["connected_revision"] == 2
    assert place("-100600")["conversation_id"] == "-100600"
    assert binds == [("1001", "-100500", ""), ("1001", "-100600", "")]
    # The turn's bot is the standalone bot now: refused, never continued through it.
    with pytest.raises(SendRefused) as moved:
        place("-100500", owner="1002", standalone="1001", topic_id="")
    assert moved.value.code == "account_not_active"


def test_a_standalone_bots_automatic_reply_keeps_the_bot_its_event_came_through(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    event = parse_telegram_update(_group_update(90), bot_account_id="2002")
    store.commit_update(90, event)
    lease = store.claim_inbox()
    store.record_submission(lease.event_id, binding_id="b" * 32, turn_ref="turn-1", outcome="message", text="hello",
                            work_ref="", delivery_reporting_version=1)
    # The reply names its event's bot whatever the caller passed: sent by that bot or not at all.
    assert store.outbox_payload(f"presence:{lease.event_id}:turn")["account_id"] == "2002"
    # A row an earlier version queued without a bot stays as it was: not migrated, not guessed.
    store.enqueue_outbox("presence:legacy:turn", _row())
    assert "account_id" not in store.outbox_payload("presence:legacy:turn")
    store.close()


def test_standalone_legacy_automatic_reply_keeps_its_reporting_account_but_an_unnamed_row_is_not_pinned(tmp_path):
    runtime = TelegramTransportRuntime(state_dir=tmp_path, settings_dir=tmp_path, token_provider=lambda: "",
                                       logger=None, submitter=SimpleNamespace(places_version=1))
    runtime._bot_id = "2002"
    legacy = {**_row(), "_reporting": {"version": 1, "account_id": "2002", "origin": {
        "kind": "automatic", "task_id": "turn-1", "source_event_id": "telegram:2002:90",
    }}}
    runtime.store.enqueue_outbox("presence:telegram:2002:90:turn", legacy)
    retained = runtime.store.claim_outbox().payload
    assert "account_id" not in retained
    assert runtime._pinned_account(retained) == "2002"
    assert runtime._ensure_pinned_account(retained) == "2002"
    assert runtime._pinned_account(_row()) == ""
    tool_report = {"version": 1, "account_id": "2002", "origin": {"kind": "tool"}}
    assert runtime._pinned_account({**_row(), "_reporting": tool_report}) == ""
    runtime.store.close()


def _owner(state: str, **facts) -> dict:
    payload = {"runtime_state": state, "bot_label": "@owner_bot", **facts}
    return {**payload, **summarize_owner(payload)}


def test_the_card_needs_no_second_token_for_the_owner_bot_and_names_each_bots_problems():
    no_token = {"runtime_state": "waiting_for_token", "connect": None, "connect_unavailable": "Bot token missing"}
    alone = card_summary(no_token, _owner("feed_off"))
    assert alone["text"] == "Not set up · groups are off for your own bot" and alone["tone"] == "warn"
    assert "separate_history" not in alone
    assert not any("token" in item["headline"].lower() for item in alone["attention"])
    waiting = card_summary(no_token, _owner("waiting_for_owner_bot", feed_reason="bridge_not_ready"))
    assert waiting["text"] == "Waiting for your bot · Ouroboros's own Telegram connection is not running"
    # Places keeps reading the standalone bot's own line.
    assert summarize(no_token)["text"] == "Not set up · the bot token is missing"

    working = {"runtime_state": "polling", "poll_result": "ok", "connect": [], "host_checked": True,
               "recent_problems": {}}
    owner = _owner("polling", poll_result="ok", recent_problems={"outbound": {"uncertain": 1}})
    both = card_summary(working, owner)
    assert both["text"] == "Connected · your bot @owner_bot: outcome unknown for 1 outgoing item"
    assert both["tone"] == "warn" and both["attention"][0]["bot"] == "owner"
    # With a working separate bot, the owner's bot being off is no problem of the card.
    assert card_summary(working, _owner("feed_off"))["text"] == "Connected · nothing needs attention"
    empty_bridge = _owner("waiting_for_owner_bot", feed_reason="bridge_not_ready", store="none")
    assert card_summary(working, empty_bridge)["text"] == "Connected · nothing needs attention"
    bridge_with_history = _owner("waiting_for_owner_bot", feed_reason="bridge_not_ready", store="read")
    assert "not running" in card_summary(working, bridge_with_history)["text"]
    owner_only = card_summary(no_token, _owner("polling", poll_result="ok", store="unreadable"))
    assert owner_only["text"] == "Receiving through Ouroboros · could not read this bot's delivery history"
    waiting_b = summarize({"runtime_state": "not_started", "ready_wait": "Waiting for Ouroboros to confirm this bot."})
    assert "waiting for Ouroboros to confirm this bot" in waiting_b["text"]


def test_old_separate_queue_records_stay_visible_with_no_token(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    try:
        # The old queue could hold a message without a bot ID. Status reads it; it does not assign one.
        store.enqueue_outbox("old-pending", _row())
        counts, facts = store.read_status()
        payload = {**counts, **facts, "runtime_state": "waiting_for_token", "connect": None,
                   "connect_unavailable": "Bot token missing"}
        owner = _owner("polling", poll_result="ok")
        assert card_summary(payload, owner)["separate_history"] is True
        assert not card_summary(payload, owner)["attention"]

        assert store.claim_outbox().delivery_id == "old-pending"
        store.mark_outbox_failed("old-pending", reason="earlier bot unavailable")
        counts, facts = store.read_status()
        payload.update(counts, **facts)
        card = card_summary(payload, owner)
        assert card["separate_history"] is True
        assert card["tone"] == "error"
        assert card["text"] == "Receiving through Ouroboros · separate bot: 1 outgoing item not delivered"
        assert [(item["kind"], item["bot"]) for item in card["attention"]] == [("not_delivered", "separate")]
        receipt = store.delivery_receipt("old-pending")
        assert receipt["state"] == "failed" and "account_id" not in receipt

        # Beyond the recent attention window the total still carries the failure into the card's Details.
        counts, facts = store.read_status(now=10**10)
        payload.update(counts, **facts)
        assert card_summary(payload, owner)["separate_history"] is True
        assert not card_summary(payload, owner)["attention"]
        assert payload["outbox_failed"] == 1
    finally:
        store.close()
