from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

from lib.events import parse_socket_envelope
from lib.host_adapter import HostBindingTerminalError, HostDelivery, HostTurnStatus
from lib.runtime import InboundWorker, OutboundWorker
from lib.slack_api import SlackApiError
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

    async def stage_private_files(self, files, *, destination):
        del files, destination
        return ()

    async def resolve_target(self, target):
        return target

    async def post_message(self, *, channel, text, thread_ts=""):
        self.posts.append((channel, text, thread_ts))
        return {"ok": True, "ts": "2.2"}


class _RejectedHost:
    available = True

    async def submit(self, _event):
        raise HostBindingTerminalError(
            "Presence binding was rejected by Host (HTTP 403)"
        )


class _FailingSlack(_Slack):
    async def post_message(self, *, channel, text, thread_ts=""):
        del channel, text, thread_ts
        raise SlackApiError("temporary_failure")


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
    def __init__(self, body) -> None:
        self.body = body

    async def json(self):
        return self.body


def test_plugin_registers_companion_operational_widget_and_durable_send(
    tmp_path,
) -> None:
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)

    assert api.companions == ["slack_socket_mode"]
    assert "slack_send" in api.tools
    assert "status" in api.routes
    assert api.tabs["slack_presence"][1]["render"]["kind"] == "declarative"
    metrics = api.tabs["slack_presence"][1]["render"]["components"][2]["components"]
    assert {item.get("path") for item in metrics} >= {
        "inbox_failed",
        "outbox_failed",
    }

    handler, _metadata = api.tools["slack_send"]
    result = handler(channel_or_user="C1", text="hello", request_id="dedupe-1")
    repeated = handler(
        channel_or_user="C1",
        text="a different message " * 1000,
        request_id="dedupe-1",
    )
    assert result["state"] == "queued"
    assert repeated["chunks_queued"] == 1
    assert BridgeStore(tmp_path).status()["outbox_pending"] == 1


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
