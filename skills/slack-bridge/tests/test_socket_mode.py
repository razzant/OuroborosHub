from __future__ import annotations

import asyncio
import json

import pytest

from lib.runtime import BridgeRuntime
from lib.socket_mode import SocketModeClient
from lib.store import BridgeStore


class _Slack:
    pass


class _WebSocket:
    def __init__(self, store: BridgeStore) -> None:
        self.store = store
        self.sent: list[dict] = []

    async def send(self, value: str) -> None:
        assert self.store.status()["inbox_pending"] == 1
        self.sent.append(json.loads(value))


def _raw(event_type: str = "message") -> str:
    return json.dumps(
        {
            "type": "events_api",
            "envelope_id": "env-1",
            "payload": {
                "event_id": "Ev-1",
                "team_id": "T1",
                "event": {
                    "type": event_type,
                    "user": "U1",
                    "channel": "C1",
                    "ts": "1.1",
                    "text": "hello",
                },
            },
        }
    )


@pytest.mark.parametrize("event_type", ["message", "app_mention"])
def test_socket_ack_happens_only_after_durable_insert(tmp_path, event_type) -> None:
    asyncio.run(_socket_ack_happens_only_after_durable_insert(tmp_path, event_type))


async def _socket_ack_happens_only_after_durable_insert(tmp_path, event_type) -> None:
    store = BridgeStore(tmp_path)
    websocket = _WebSocket(store)
    socket = SocketModeClient(
        _Slack(),
        store,
        bot_user_id="U_BOT",
    )

    await socket.handle_raw_message(websocket, _raw(event_type))

    assert websocket.sent == [{"envelope_id": "env-1"}]


@pytest.mark.parametrize("event_type", ["message", "app_mention"])
def test_failed_durable_insert_is_not_acknowledged(tmp_path, monkeypatch, event_type) -> None:
    asyncio.run(_failed_durable_insert_is_not_acknowledged(tmp_path, monkeypatch, event_type))


async def _failed_durable_insert_is_not_acknowledged(tmp_path, monkeypatch, event_type) -> None:
    store = BridgeStore(tmp_path)
    websocket = _WebSocket(store)
    socket = SocketModeClient(
        _Slack(),
        store,
        bot_user_id="U_BOT",
    )

    def fail(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "ingest_envelope", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        await socket.handle_raw_message(websocket, _raw(event_type))
    assert websocket.sent == []


@pytest.mark.parametrize("channels, marked", [(["C1"], True), (["C2"], False), ([], False)])
def test_socket_marks_new_roots_only_in_its_configured_channels(tmp_path, channels, marked) -> None:
    async def run():
        store = BridgeStore(tmp_path)
        socket = SocketModeClient(_Slack(), store, bot_user_id="U_BOT", new_root_channels=channels)
        await socket.handle_raw_message(_WebSocket(store), _raw())
        item = store.claim_inbox()
        assert item is not None and item.text == "hello"
        assert item.structured.get("new_root_signal") is (True if marked else None)
    asyncio.run(run())


def test_runtime_hands_its_configured_channels_to_the_socket(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    runtime = BridgeRuntime(store=store, slack=_Slack(), host=object(), bot_user_id="U_BOT",
                            new_root_channels=["C1", "G2", "C1"])
    assert runtime.socket.new_root_channels == frozenset({"C1", "G2"})
    assert BridgeRuntime(store=store, slack=_Slack(), host=object(),
                         bot_user_id="U_BOT").socket.new_root_channels == frozenset()
