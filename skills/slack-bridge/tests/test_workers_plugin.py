from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import types
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from starlette.requests import Request

from lib.events import parse_socket_envelope
from lib.host_adapter import HostBindingTerminalError, HostDelivery, HostTurnStatus
from lib.runtime import InboundWorker, OutboundWorker
from lib.slack_api import SlackApiError, SlackClient, SlackMutationUncertain
from lib.store import BridgeStore


class _Host:
    available = True

    def __init__(self) -> None:
        self.events = []

    async def submit(self, event):
        self.events.append(event)
        return "turn-1"

    async def status(self, reference):
        assert reference == "turn-1"
        return HostTurnStatus("ready")

    async def deliver(self, reference):
        assert reference == "turn-1"
        return HostDelivery(("response",))


class _DeferredHost:
    available = True

    def __init__(self) -> None:
        self.polls = 0

    async def submit(self, _event):
        return "deferred:durable-reference"

    async def status(self, _reference):
        self.polls += 1
        state = "ready" if self.polls >= 3 else "pending"
        return HostTurnStatus(state, texts=("working",))

    async def deliver(self, _reference):
        return HostDelivery(("late reply",))


class _Slack:
    def __init__(self) -> None:
        self.posts = []
        self.formats = []

    async def stage_private_files(self, files, *, destination):
        del files, destination
        return ()

    async def user_info(self, user_id):
        return {"id": user_id, "name": "reader", "profile": {"display_name": "Reader"}}

    async def conversation_info(self, channel_id):
        return {"id": channel_id, "is_im": True}

    async def resolve_target(self, target):
        return target

    async def post_message(self, *, channel, text, thread_ts="", text_format="markdown"):
        self.posts.append((channel, text, thread_ts))
        self.formats.append(text_format)
        return {"ok": True, "ts": "2.2"}


class _RejectedHost:
    available = True

    async def submit(self, _event):
        raise HostBindingTerminalError(
            "Presence binding was rejected by Host (HTTP 403)"
        )


class _FailingSlack(_Slack):
    async def post_message(self, *, channel, text, thread_ts="", text_format="markdown"):
        del channel, text, thread_ts, text_format
        raise SlackApiError("temporary_failure")


def test_inbound_lease_covers_maximum_presence_turn(monkeypatch, tmp_path) -> None:
    store = BridgeStore(tmp_path)
    payload = _payload()
    store.ingest_envelope(payload, parse_socket_envelope(payload))
    claimed = {}
    original_claim = store.claim_inbox

    def claim(*, lease_seconds):
        claimed["seconds"] = lease_seconds
        return original_claim(lease_seconds=lease_seconds)

    monkeypatch.setattr(store, "claim_inbox", claim)
    inbound = InboundWorker(store, _Slack(), _Host(), staged_root=tmp_path / "staged")
    assert asyncio.run(inbound.process_once()) is True
    assert claimed["seconds"] >= 1800.0


def _payload() -> dict:
    return {
        "type": "events_api",
        "envelope_id": "env-1",
        "payload": {
            "event_id": "Ev-1",
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "U1",
                "channel": "D1",
                "ts": "1.1",
                "text": "/status remains conversation text",
            },
        },
    }


def test_host_adapter_flow_preserves_text_and_queues_threaded_reply(tmp_path) -> None:
    asyncio.run(_host_adapter_flow_preserves_text_and_queues_threaded_reply(tmp_path))


async def _host_adapter_flow_preserves_text_and_queues_threaded_reply(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    payload = _payload()
    store.ingest_envelope(payload, parse_socket_envelope(payload))
    host = _Host()
    slack = _Slack()

    inbound = InboundWorker(store, slack, host, staged_root=tmp_path / "staged")
    assert await inbound.process_once() is True
    assert host.events[0].text == "/status remains conversation text"
    assert host.events[0].actor_user_id == "U1"
    assert store.status()["inbox_delivered"] == 1
    assert store.status()["outbox_pending"] == 1

    outbound = OutboundWorker(store, slack)
    assert await outbound.process_once() is True
    assert slack.posts == [("D1", "response", "1.1")]
    assert slack.formats == ["markdown"]
    assert store.status()["outbox_delivered"] == 1


def test_deferred_ack_is_queued_once_before_eventual_reply(tmp_path) -> None:
    asyncio.run(_deferred_ack_is_queued_once_before_eventual_reply(tmp_path))


async def _deferred_ack_is_queued_once_before_eventual_reply(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    payload = _payload()
    store.ingest_envelope(payload, parse_socket_envelope(payload))
    original_retry = store.retry_inbox

    def retry_now(row_id, lease_token, error, *, delay_seconds):
        del delay_seconds
        original_retry(row_id, lease_token, error, delay_seconds=0)

    store.retry_inbox = retry_now
    inbound = InboundWorker(
        store,
        _Slack(),
        _DeferredHost(),
        staged_root=tmp_path / "staged",
    )

    assert await inbound.process_once() is True
    assert await inbound.process_once() is True
    assert await inbound.process_once() is True

    messages = []
    while item := store.claim_outbox():
        messages.append(item.text)
        store.complete_outbox(item.row_id, item.lease_token, provider_message_ts="ok")
    assert messages == ["working", "late reply"]
    assert store.status()["inbox_delivered"] == 1


def test_binding_rejection_terminally_fails_inbox_without_retry(tmp_path) -> None:
    async def run() -> None:
        store = BridgeStore(tmp_path)
        payload = _payload()
        store.ingest_envelope(payload, parse_socket_envelope(payload))
        worker = InboundWorker(
            store, _Slack(), _RejectedHost(), staged_root=tmp_path / "staged"
        )

        assert await worker.process_once() is True
        status = store.status()
        assert status["inbox_failed"] == 1
        assert status["inbox_pending"] == 0
        assert "HTTP 403" in status["last_delivery_error"]

    asyncio.run(run())


def test_outbox_stops_after_five_attempts_and_later_message_can_run(tmp_path) -> None:
    async def run() -> None:
        store = BridgeStore(tmp_path)
        store.enqueue_outbox(
            request_id="first", target="C1", thread_ts="1.1", chunks=("first",)
        )
        store.enqueue_outbox(
            request_id="second", target="C1", thread_ts="1.1", chunks=("second",)
        )
        original_retry = store.retry_outbox

        def retry_now(row_id, lease_token, error, *, delay_seconds):
            del delay_seconds
            original_retry(row_id, lease_token, error, delay_seconds=0)

        store.retry_outbox = retry_now
        worker = OutboundWorker(store, _FailingSlack())
        for _ in range(5):
            assert await worker.process_once() is True

        status = store.status()
        assert status["outbox_failed"] == 1
        assert status["outbox_pending"] == 1
        later = store.claim_outbox()
        assert later is not None and later.text == "second"

    asyncio.run(run())


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("slack_bridge_test")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_test.plugin", root / "plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Api:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.companions = []
        self.tools = {}
        self.routes = {}
        self.tabs = {}
        self.settings = {}

    def get_state_dir(self):
        return str(self.state_dir)

    def get_settings(self, keys):
        return {key: "" for key in keys}

    def register_companion_process(self, name):
        self.companions.append(name)

    def register_tool(self, name, handler, **metadata):
        self.tools[name] = (handler, metadata)

    def register_route(self, name, handler, methods=("GET",)):
        self.routes[name] = (handler, methods)

    def register_ui_tab(self, name, title, **metadata):
        self.tabs[name] = (title, metadata)

    def register_settings_section(self, name, title, schema):
        self.settings[name] = (title, schema)


class _Request:
    def __init__(self, body, method="POST") -> None:
        self.body = body
        self.method = method

    async def json(self):
        return self.body


def test_plugin_registers_companion_operational_widget_and_durable_send(
    tool_json,
    tmp_path,
) -> None:
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)

    assert api.companions == ["slack_socket_mode"]
    assert "slack_send" in api.tools
    format_schema = api.tools["slack_send"][1]["schema"]["properties"]["text_format"]
    assert format_schema["enum"] == ["markdown", "mrkdwn", "plain"]
    assert format_schema["default"] == "markdown"
    assert "status" in api.routes
    assert api.tabs["slack_presence"][1]["render"]["kind"] == "declarative"
    metrics = api.tabs["slack_presence"][1]["render"]["components"][2]["components"]
    assert {item.get("path") for item in metrics} >= {
        "inbox_failed",
        "outbox_failed",
    }

    handler, _metadata = api.tools["slack_send"]
    result = tool_json(handler(channel_or_user="C1", text="hello", request_id="dedupe-1"))
    repeated = tool_json(handler(
        channel_or_user="C1",
        text="a different message " * 1000,
        request_id="dedupe-1",
    ))
    assert result["state"] == "queued"
    assert repeated["chunks_queued"] == 1
    assert BridgeStore(tmp_path).status()["outbox_pending"] == 1


def test_slack_send_explicit_plain_format_is_persisted(tool_json, tmp_path):
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)
    handler, _metadata = api.tools["slack_send"]
    result = tool_json(handler(channel_or_user="D1", text="**literal**", text_format="plain", request_id="literal"))
    assert result["state"] == "queued"
    item = BridgeStore(tmp_path).claim_outbox()
    assert item.text_format == "plain" and item.text == "**literal**"


def test_registered_tool_arrays_have_items_and_enums_have_no_empty_choices(tmp_path):
    """Pin the two provider catalog refusals against actual registrations."""
    api = _Api(tmp_path)
    _load_plugin().register(api)
    pending = [(name, metadata["schema"]) for name, (_, metadata) in api.tools.items()]
    while pending:
        path, node = pending.pop()
        if isinstance(node, dict):
            if node.get("type") == "array":
                assert isinstance(node.get("items"), dict) and node["items"], path
            if "enum" in node:
                assert node["enum"] and "" not in node["enum"], path
            pending.extend((f"{path}.{key}", value) for key, value in node.items())
        elif isinstance(node, list):
            pending.extend((f"{path}[{index}]", value) for index, value in enumerate(node))


def test_message_edit_block_fields_survive_registration_queue_and_provider_request(tool_json, tmp_path):
    api = _Api(tmp_path)
    _load_plugin().register(api)
    edit, metadata = api.tools["slack_message_edit"]
    block_schema = metadata["schema"]["properties"]["blocks"]["items"]
    assert block_schema["type"] == "object"
    assert block_schema["properties"]["type"]["type"] == "string"
    assert block_schema["required"] == ["type"]
    assert block_schema.get("additionalProperties", True) is True
    blocks = [
        {"type": "section", "block_id": "summary", "text": {"type": "mrkdwn", "text": "*Kept*"},
         "accessory": {"type": "button", "action_id": "open", "text": {"type": "plain_text", "text": "Open"}}},
        {"type": "divider"},
    ]
    captured = []

    def provider(request):
        captured.append(json.loads(request.content))
        assert request.url.path == "/api/chat.update"
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            client = SlackClient("xoxb-test", "xapp-test", http_client=http)
            for index, value in enumerate((blocks, [])):
                assert tool_json(edit(channel="C1", ts="1.1", blocks=value, text="fallback",
                                      text_format="plain", request_id=f"blocks-{index}"))["ok"]
                assert await OutboundWorker(BridgeStore(tmp_path), client).process_once()

    asyncio.run(run())
    assert captured == [
        {"channel": "C1", "ts": "1.1", "blocks": value, "text": "fallback", "mrkdwn": False}
        for value in (blocks, [])
    ]


def test_slack_file_upload_copies_immutable_bytes_into_existing_outbox(tool_json, tmp_path):
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)
    source = tmp_path / "source.txt"
    source.write_bytes(b"bytes before enqueue")
    handler, _metadata = api.tools["slack_file_upload"]
    result = tool_json(handler(file_path=str(source), channel_id="C1", request_id="upload-1"))
    assert result["state"] == "queued"
    source.write_bytes(b"changed after enqueue")
    repeated = tool_json(handler(file_path=str(source), channel_id="C2", request_id="upload-1"))
    assert repeated["deduplicated"] is True
    item = BridgeStore(tmp_path).claim_outbox()
    assert item is not None and item.kind == "mutation" and item.operation == "upload_file"
    assert Path(item.payload["path"]).read_bytes() == b"bytes before enqueue"


def _upload_provider(calls):
    """Fake Slack External Upload API behind the real SlackClient."""

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files.getUploadURLExternal"):
            calls.append(("start", dict(parse_qsl(request.content.decode("utf-8")))))
            return httpx.Response(200, json={"ok": True, "upload_url": "https://uploads.example/u", "file_id": "F1"})
        if request.url.host == "uploads.example":
            calls.append(("bytes", request.content))
            return httpx.Response(200, text="ok")
        assert request.url.path.endswith("/files.completeUploadExternal")
        calls.append(("complete", json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "files": [{"id": "F1"}]})

    return provider


def _deliver_outbox(state: Path, calls: list) -> None:
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_upload_provider(calls))) as http:
            worker = OutboundWorker(BridgeStore(state), SlackClient("xoxb-test", "xapp-test", http_client=http))
            assert await worker.process_once()
            assert not await worker.process_once()

    asyncio.run(run())


_OPAQUE_REQUEST_IDS = {
    "slash": "report/2026-10",
    "traversal": "../../../escape",
    "absolute": None,  # a path inside this test's own tmp_path
    "windows_absolute": "C:\\temp\\absolute",
    "windows_traversal": "..\\..\\escape",
    "drive_relative": "C:escape",
    "colon": "upload:1",
    "long": "x" * 1000,
    "unicode": "идея/с/слэшем",
}


@pytest.mark.parametrize("kind", list(_OPAQUE_REQUEST_IDS))
def test_upload_request_id_is_only_an_outbox_key(tool_json, tmp_path, kind):
    # Nested so that even the pre-fix traversal could not leave tmp_path.
    state = tmp_path / "a" / "b" / "state"
    state.mkdir(parents=True)
    request_id = _OPAQUE_REQUEST_IDS[kind] or str(tmp_path / "absolute" / "x")
    source = tmp_path / "source" / "Отчёт Q3 (final).txt"
    source.parent.mkdir()
    source.write_bytes(b"first bytes")
    module = _load_plugin()
    api = _Api(state)
    module.register(api)
    upload, _metadata = api.tools["slack_file_upload"]

    def files():
        return sorted(
            path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")
            if path.is_file() and not path.name.startswith("slack_bridge.sqlite3")
        )

    first = tool_json(upload(file_path=str(source), channel_id="C1", title="Title", request_id=request_id))
    source.write_bytes(b"second bytes")
    retry = tool_json(upload(file_path=str(source), channel_id="C2", title="Other", request_id=request_id))
    assert first["ok"] and retry["ok"]
    assert first["request_id"] == retry["request_id"] == request_id
    assert first["deduplicated"] is False and retry["deduplicated"] is True
    staged = list((state / "outbound" / "files").iterdir())
    assert len(staged) == 1 and staged[0].read_bytes() == b"first bytes"
    assert re.fullmatch(r"[0-9a-f]{32}", staged[0].name[:32])
    assert staged[0].name[32:] == "-" + module._safe_name(source.name)
    assert files() == sorted(["source/" + source.name, staged[0].relative_to(tmp_path).as_posix()])

    calls = []
    _deliver_outbox(state, calls)
    assert calls == [
        ("start", {"filename": source.name, "length": str(len(b"first bytes"))}),
        ("bytes", b"first bytes"),
        ("complete", {"files": [{"id": "F1", "title": "Title"}], "channel_id": "C1"}),
    ]
    receipt = BridgeStore(state).delivery_receipt(request_id)
    assert receipt["request_id"] == request_id and receipt["operation"] == "upload_file"
    assert [part["state"] for part in receipt["parts"]] == ["delivered"]
    assert files() == ["source/" + source.name]


def test_upload_queued_with_a_legacy_staged_name_is_delivered_as_stored(tmp_path):
    # Slack Bridge 1.4.3 named staged copies after the request ID. The worker
    # only follows the stored path, so those rows need no migration.
    staged = tmp_path / "outbound" / "files" / "legacy-1-0123456789ab-report.txt"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"legacy bytes")
    BridgeStore(tmp_path).enqueue_mutation(
        request_id="legacy-1", operation="upload_file",
        payload={"path": str(staged), "filename": "report.txt", "title": "", "channel": "C1",
                 "thread_ts": "", "initial_comment": ""},
    )
    _load_plugin().register(_Api(tmp_path))

    calls = []
    _deliver_outbox(tmp_path, calls)
    assert calls == [
        ("start", {"filename": "report.txt", "length": str(len(b"legacy bytes"))}),
        ("bytes", b"legacy bytes"),
        ("complete", {"files": [{"id": "F1", "title": "report.txt"}], "channel_id": "C1"}),
    ]
    assert not staged.exists()
    assert BridgeStore(tmp_path).delivery_receipt("legacy-1")["parts"][0]["state"] == "delivered"


def test_upload_completion_transport_loss_is_terminally_uncertain(tmp_path):
    class _UncertainSlack(_Slack):
        async def upload_file(self, **_payload):
            raise SlackMutationUncertain("complete_response_lost")

    store = BridgeStore(tmp_path)
    store.enqueue_mutation(
        request_id="upload-uncertain", operation="upload_file",
        payload={"path": str(tmp_path / "staged"), "filename": "file.txt", "channel": "C1"},
    )
    assert asyncio.run(OutboundWorker(store, _UncertainSlack()).process_once()) is True
    status = store.status()
    assert status["mutations_uncertain"] == 1 and status["mutations_pending"] == 0


def test_generic_write_transport_loss_is_uncertain_without_false_delivery(tmp_path):
    class _GenericUncertainSlack(_Slack):
        async def generic_request(self, **_payload):
            raise SlackMutationUncertain("provider_response_lost")

    store = BridgeStore(tmp_path)
    store.enqueue_mutation(
        request_id="generic-uncertain", operation="generic_api",
        payload={"method": "POST", "path": "chat.postMessage", "body": {"channel": "C1", "text": "hello"}},
    )
    assert asyncio.run(OutboundWorker(store, _GenericUncertainSlack()).process_once()) is True
    status = store.status()
    assert status["mutations_uncertain"] == 1 and status["mutations_delivered"] == 0


def test_generic_slack_api_post_is_registered_and_uses_existing_mutation_outbox(tool_json, tmp_path):
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)
    handler, metadata = api.tools["slack_api"]
    assert metadata["schema"]["properties"]["method"]["enum"] == ["GET", "POST"]
    result = tool_json(asyncio.run(handler(
        method="POST", path="chat.postMessage", body={"channel": "C1", "text": "hello"}, request_id="generic-1"
    )))
    assert result["state"] == "queued"
    item = BridgeStore(tmp_path).claim_outbox()
    assert item is not None and item.kind == "mutation" and item.operation == "generic_api"
    assert item.payload["path"] == "chat.postMessage"
    rejected = tool_json(asyncio.run(handler(method="POST", path="chat.postMessage", body={"token": "secret"}, request_id="generic-secret")))
    assert rejected["ok"] is False and "token" in rejected["error"]["message"]


def test_generic_slack_api_registered_get_read_returns_provider_response(tool_json, tmp_path):
    module = _load_plugin()

    class _GenericClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @staticmethod
        def normalize_method_path(method, path):
            return method.upper(), path.removeprefix("/api/")

        async def generic_request(self, **_kwargs):
            return {"ok": True, "channels": [{"id": "C1"}]}

    module.SlackClient = _GenericClient
    api = _Api(tmp_path)
    module.register(api)
    handler, _metadata = api.tools["slack_api"]
    result = tool_json(asyncio.run(handler(method="GET", effect="read", path="/api/conversations.list", params={"limit": 1})))
    assert result == {"ok": True, "state": "read", "source": "conversations.list", "response": {"ok": True, "channels": [{"id": "C1"}]}}


def test_generic_get_write_is_queued_and_get_read_is_direct(tool_json, tmp_path):
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)
    handler = api.tools["slack_api"][0]
    queued = tool_json(asyncio.run(handler(method="GET", path="auth.revoke", request_id="get-write")))
    assert queued["state"] == "queued"
    item = BridgeStore(tmp_path).claim_outbox()
    assert item.kind == "mutation" and item.payload["method"] == "GET"
    assert item.payload["effect"] == "write" and item.payload["path"] == "auth.revoke"


def test_generic_timeout_cannot_replay_and_rate_limit_waits(tmp_path):
    async def run():
        calls = []
        def timeout_provider(request):
            calls.append(request)
            raise httpx.ReadTimeout("accepted but response lost", request=request)
        store = BridgeStore(tmp_path / "uncertain")
        store.enqueue_mutation(request_id="timeout", operation="generic_api",
                               payload={"method": "POST", "path": "chat.postMessage",
                                        "body": {"channel": "C1", "text": "hello"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_provider)) as http:
            client = SlackClient("xoxb-test", "xapp-test", http_client=http)
            worker = OutboundWorker(store, client)
            assert await worker.process_once()
            assert not await worker.process_once()
        assert len(calls) == 1 and store.status()["mutations_uncertain"] == 1
        uncertain = store.delivery_receipt("timeout")
        assert uncertain["parts"][0]["state"] == "uncertain"
        assert uncertain["parts"][0]["provider_result"]["uncertain"] is True
        def limited(_request):
            return httpx.Response(429, headers={"Retry-After": "30"}, json={"ok": False, "error": "ratelimited"})
        rate_store = BridgeStore(tmp_path / "rate")
        rate_store.enqueue_mutation(request_id="rate", operation="generic_api",
                                    payload={"method": "POST", "path": "chat.postMessage",
                                             "body": {"channel": "C1", "text": "hello"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as http:
            client = SlackClient("xoxb-test", "xapp-test", http_client=http)
            assert await OutboundWorker(rate_store, client).process_once()
        row = rate_store.status()
        assert row["mutations_pending"] == 1 and row["mutations_failed"] == 0
        with rate_store._connect() as db:
            due = db.execute("SELECT available_at FROM outbox WHERE request_id='rate'").fetchone()[0]
        assert due > __import__("time").time() + 25
    asyncio.run(run())


def test_generic_provider_speech_reports_actual_message_while_other_effect_does_not(tool_json, tmp_path):
    async def run():
        module = _load_plugin()
        api = _Api(tmp_path)
        module.register(api)
        store = BridgeStore(tmp_path)
        store.set_runtime(presence_delivery_version=1, workspace_id="T1")
        ctx = types.SimpleNamespace(task_id="task-1", task_metadata={"presence": {"event": {"source_event_id": "event-1"}}})
        handler = api.tools["slack_api"][0]
        assert tool_json(await handler(ctx, method="POST", path="chat.postMessage",
                                       body={"channel": "C1", "text": "hello"}, request_id="speech"))["state"] == "queued"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(
            200, json={"ok": True, "channel": "C1", "ts": "100.2", "message": {"text": "hello"}}
        ))) as http:
            assert await OutboundWorker(store, SlackClient("xoxb-test", "xapp-test", http_client=http)).process_once()
        with store._connect() as db:
            row = db.execute("SELECT target,resolved_channel,report_state,report_payload_json,result_json FROM outbox WHERE request_id='speech'").fetchone()
        report = json.loads(row["report_payload_json"])
        if importlib.util.find_spec("ouroboros"):
            from ouroboros.presence_delivery import validate_delivery
            assert validate_delivery(report) == report
        assert row["target"] == "C1" and row["resolved_channel"] == "C1"
        assert row["report_state"] == "pending" and report["text"] == "hello"
        assert report["account_id"] == "T1" and report["origin"]["source_event_id"] == "event-1"
        assert json.loads(row["result_json"])["ts"] == "100.2"
        receipt = tool_json(api.tools["slack_receipt"][0](request_id="speech"))
        assert receipt["parts"][0]["provider_result"]["ts"] == "100.2"
        assert receipt["parts"][0]["history_report_state"] == "pending"
        assert tool_json(await handler(method="GET", path="auth.revoke", request_id="operation"))["state"] == "queued"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(
            200, json={"ok": True, "revoked": True}
        ))) as http:
            assert await OutboundWorker(store, SlackClient("xoxb-test", "xapp-test", http_client=http)).process_once()
        with store._connect() as db:
            row = db.execute("SELECT report_state,result_json FROM outbox WHERE request_id='operation'").fetchone()
        assert row["report_state"] == "" and json.loads(row["result_json"])["revoked"] is True
        assert tool_json(await handler(method="POST", path="chat.update", result_kind="operation",
                                       body={"channel": "C1", "ts": "100.2", "text": "fixed"},
                                       request_id="edit"))["state"] == "queued"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(
            200, json={"ok": True, "channel": "C1", "ts": "100.2", "message": {"text": "fixed"}}
        ))) as http:
            assert await OutboundWorker(store, SlackClient("xoxb-test", "xapp-test", http_client=http)).process_once()
        with store._connect() as db:
            row = db.execute("SELECT report_state,result_json FROM outbox WHERE request_id='edit'").fetchone()
        assert row["report_state"] == "" and json.loads(row["result_json"])["ok"] is True
        assert tool_json(api.tools["slack_receipt"][0](request_id="edit"))["parts"][0]["state"] == "delivered"
    asyncio.run(run())


def test_settings_accept_only_canonical_binding_ids(tmp_path) -> None:
    async def run() -> None:
        module = _load_plugin()
        api = _Api(tmp_path)
        module.register(api)
        handler, _methods = api.routes["settings/save"]

        invalid = await handler(_Request({"binding_id": "binding-1"}))
        assert invalid.status_code == 400
        assert not (tmp_path / "settings.json").exists()

        binding_id = "0123456789abcdef" * 2
        valid = await handler(_Request({"binding_id": binding_id}))
        assert valid.status_code == 200
        saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert saved["binding_id"] == binding_id

    asyncio.run(run())


def test_outbound_ack_can_arrive_before_host_turn_completes(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowHost(_Host):
            async def submit(self, event):
                entered.set()
                await release.wait()
                return await super().submit(event)

        store, slack = BridgeStore(tmp_path), _Slack()
        payload = _payload()
        store.ingest_envelope(payload, parse_socket_envelope(payload))
        inbound = InboundWorker(
            store, slack, SlowHost(), staged_root=tmp_path / "staged"
        )
        task = asyncio.create_task(inbound.process_once())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            store.enqueue_outbox(
                request_id="early-ack",
                target="D1",
                thread_ts="1.1",
                chunks=("Reading it now",),
            )
            assert await OutboundWorker(store, slack).process_once()
            assert not task.done()
            assert slack.posts == [("D1", "Reading it now", "1.1")]
        finally:
            release.set()
            await task

    asyncio.run(run())


def test_slow_host_keeps_exclusive_inbox_lease_past_ninety_seconds(
    monkeypatch, tmp_path
):
    import lib.store as store_module

    clock = [1000.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])

    async def run():
        store = BridgeStore(tmp_path)
        payload = _payload()
        store.ingest_envelope(payload, parse_socket_envelope(payload))
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowHost(_Host):
            async def submit(self, event):
                entered.set()
                await release.wait()
                return await super().submit(event)

        task = asyncio.create_task(
            InboundWorker(
                store, _Slack(), SlowHost(), staged_root=tmp_path / "staged"
            ).process_once()
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            clock[0] += 1801.0
            assert store.claim_inbox() is None
        finally:
            release.set()
            await task

    asyncio.run(run())


def test_absent_settings_are_missing_while_unreadable_settings_say_so(tmp_path):
    async def run():
        module = _load_plugin()
        api = _Api(tmp_path)
        module.register(api)
        status_handler, _methods = api.routes["status"]

        absent = json.loads((await status_handler()).body)
        assert absent["binding_state"] == "missing"
        assert absent["has_presence_binding"] is False
        assert absent["local_settings_error"] == ""

        for corrupt in (b"{not json", b'["a list is not settings"]', b"\xff\xfe binary"):
            (tmp_path / "settings.json").write_bytes(corrupt)
            payload = json.loads((await status_handler()).body)
            assert payload["binding_state"] == "unreadable", corrupt
            assert payload["has_presence_binding"] is False
            assert payload["local_settings_error"]

    asyncio.run(run())


def test_saving_settings_refuses_to_overwrite_an_unreadable_file(tmp_path):
    async def run():
        module = _load_plugin()
        api = _Api(tmp_path)
        module.register(api)
        handler, _methods = api.routes["settings/save"]
        corrupt = b'{"binding_id": "0123456789abcdef0123456789abcdef", truncated'
        (tmp_path / "settings.json").write_bytes(corrupt)

        refused = await handler(_Request({"binding_id": "f" * 32}))
        assert refused.status_code == 409
        assert b"left unchanged" in refused.body
        # The unreadable bytes survive: a save never silently discards them.
        assert (tmp_path / "settings.json").read_bytes() == corrupt

        (tmp_path / "settings.json").unlink()
        accepted = await handler(_Request({"binding_id": "f" * 32}))
        assert accepted.status_code == 200
        assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["binding_id"] == "f" * 32

    asyncio.run(run())


def _http_request(method: str, body: bytes = b"") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": method, "path": "/settings/save",
                    "headers": [], "query_string": b""}, receive)


def _settings_route(state: Path):
    api = _Api(state)
    _load_plugin().register(api)
    handler, methods = api.routes["settings/save"]
    form = api.settings["slack_presence"][1]["components"][1]
    return handler, methods, [field["name"] for field in form["fields"]]


def _file_identity(path: Path):
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_settings_read_hydrates_the_form_without_writing(tmp_path, method):
    async def run():
        handler, methods, field_names = _settings_route(tmp_path)
        assert methods == ("GET", "POST")
        path = tmp_path / "settings.json"
        # A read that consumed this body and saved it would clear the binding.
        clearing = json.dumps({"binding_id": "", "SLACK_INBOUND_WORKERS": "9"}).encode()

        absent = await handler(_http_request(method, clearing))
        assert absent.status_code == 200
        assert json.loads(absent.body) == {
            "binding_id": "", "SLACK_INBOUND_WORKERS": "4", "SLACK_OUTBOUND_WORKERS": "2",
        }
        assert list(tmp_path.iterdir()) == []

        path.write_text(json.dumps({"binding_id": "a" * 32, "SLACK_OUTBOUND_WORKERS": 5, "unrelated": True}))
        before = _file_identity(path)
        stored = json.loads((await handler(_http_request(method, clearing))).body)
        assert stored == {"binding_id": "a" * 32, "SLACK_INBOUND_WORKERS": "4", "SLACK_OUTBOUND_WORKERS": "5"}
        assert list(stored) == field_names
        assert _file_identity(path) == before

        path.write_bytes(b'{"binding_id": "' + b"a" * 32 + b'", truncated')
        before = _file_identity(path)
        refused = await handler(_http_request(method, clearing))
        assert refused.status_code == 409
        assert _file_identity(path) == before
        assert [item.name for item in tmp_path.iterdir()] == ["settings.json"]

    asyncio.run(run())


def test_hydrated_settings_round_trip_keeps_binding_and_explicit_edits(tmp_path):
    async def run():
        handler, _methods, _fields = _settings_route(tmp_path)
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"binding_id": "a" * 32, "unrelated": True}))

        async def read():
            return json.loads((await handler(_http_request("GET"))).body)

        async def save(values):
            return await handler(_http_request("POST", json.dumps(values).encode()))

        form = await read()
        form["SLACK_OUTBOUND_WORKERS"] = "3"
        assert (await save(form)).status_code == 200
        assert json.loads(path.read_text()) == {
            "binding_id": "a" * 32, "unrelated": True,
            "SLACK_INBOUND_WORKERS": 4, "SLACK_OUTBOUND_WORKERS": 3,
        }
        assert await read() == {"binding_id": "a" * 32, "SLACK_INBOUND_WORKERS": "4", "SLACK_OUTBOUND_WORKERS": "3"}

        # Replacing or deliberately clearing the binding stays an owner choice.
        assert (await save({**form, "binding_id": "b" * 32})).status_code == 200
        assert (await read())["binding_id"] == "b" * 32
        assert (await save({**form, "binding_id": ""})).status_code == 200
        assert json.loads(path.read_text())["binding_id"] == ""

        before = path.read_bytes()
        invalid = await save({"binding_id": "binding-1", "SLACK_INBOUND_WORKERS": "7"})
        assert invalid.status_code == 400
        assert path.read_bytes() == before

    asyncio.run(run())


def test_settings_save_keeps_its_error_order_on_unreadable_settings(tmp_path):
    async def run():
        handler, _methods, _fields = _settings_route(tmp_path)
        path = tmp_path / "settings.json"
        corrupt = b'{"binding_id": "' + b"a" * 32 + b'", truncated'
        path.write_bytes(corrupt)
        # A non-object body is refused before the settings file is read.
        for body, status in ((b"[]", 400), (b"null", 400), (b"{}", 409), (b"{", 409), (b"", 409)):
            response = await handler(_http_request("POST", body))
            assert response.status_code == status, body
            assert path.read_bytes() == corrupt

    asyncio.run(run())


def test_shared_settings_reader_is_strict_for_plugin_and_companion(tmp_path):
    from lib.local_settings import LocalSettingsError, load_local_settings

    assert load_local_settings(tmp_path) == {}
    assert load_local_settings(tmp_path / "never-created") == {}

    (tmp_path / "settings.json").write_text('{"binding_id": "a"}', encoding="utf-8")
    assert load_local_settings(tmp_path) == {"binding_id": "a"}

    (tmp_path / "settings.json").write_text("[]", encoding="utf-8")
    with pytest.raises(LocalSettingsError, match="JSON object"):
        load_local_settings(tmp_path)

    (tmp_path / "settings.json").write_text("{", encoding="utf-8")
    with pytest.raises(LocalSettingsError, match="not valid JSON"):
        load_local_settings(tmp_path)

    # A directory where the file belongs is unreadable, never "not configured".
    directory = tmp_path / "as-directory"
    (directory / "settings.json").mkdir(parents=True)
    with pytest.raises(LocalSettingsError, match="could not be read"):
        load_local_settings(directory)


def test_slack_join_is_a_durable_queued_mutation_with_a_receipt(tool_json, tmp_path):
    async def run():
        module = _load_plugin()
        api = _Api(tmp_path)
        module.register(api)
        handler = api.tools["slack_join"][0]

        queued = tool_json(handler(channel_id="C1", request_id="join-1"))
        assert queued["state"] == "queued" and queued["operation"] == "join_conversation"
        assert queued["request_id"] == "join-1" and queued["deduplicated"] is False
        assert "uncertain" in queued["uncertainty"]

        repeated = tool_json(handler(channel_id="C1", request_id="join-1"))
        assert repeated["deduplicated"] is True

        store = BridgeStore(tmp_path)
        calls = []

        def provider(request):
            calls.append((request.method, request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "channel": {"id": "C1", "name": "room"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            assert await OutboundWorker(store, SlackClient("xoxb-test", "xapp-test", http_client=http)).process_once()
        assert calls == [("POST", "/api/conversations.join", {"channel": "C1"})]

        receipt = tool_json(api.tools["slack_receipt"][0](request_id="join-1"))
        assert receipt["kind"] == "mutation" and receipt["operation"] == "join_conversation"
        assert receipt["parts"][0]["state"] == "delivered"
        assert receipt["parts"][0]["provider_result"]["channel"]["id"] == "C1"
        assert store.status()["mutations_delivered"] == 1

    asyncio.run(run())


def test_slack_join_refusal_is_terminal_and_never_silently_retried(tool_json, tmp_path):
    async def run():
        module = _load_plugin()
        api = _Api(tmp_path)
        module.register(api)
        assert tool_json(api.tools["slack_join"][0](channel_id="C9", request_id="join-2"))["state"] == "queued"

        store = BridgeStore(tmp_path)
        calls = []

        def provider(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": False, "error": "already_in_channel"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            client = SlackClient("xoxb-test", "xapp-test", http_client=http)
            assert await OutboundWorker(store, client).process_once()
            assert not await OutboundWorker(store, client).process_once()
        assert len(calls) == 1
        receipt = tool_json(api.tools["slack_receipt"][0](request_id="join-2"))
        assert receipt["parts"][0]["state"] == "failed"
        assert receipt["parts"][0]["error"] == "already_in_channel"
        assert receipt["parts"][0]["provider_result"]["uncertain"] is False

    asyncio.run(run())
