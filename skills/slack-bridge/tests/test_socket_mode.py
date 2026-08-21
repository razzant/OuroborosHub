from __future__ import annotations

import asyncio
import json

import pytest

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


def _raw() -> str:
    return json.dumps(
        {
            "type": "events_api",
            "envelope_id": "env-1",
            "payload": {
                "event_id": "Ev-1",
                "team_id": "T1",
                "event": {
                    "type": "message",
                    "user": "U1",
                    "channel": "C1",
                    "ts": "1.1",
                    "text": "hello",
                },
            },
        }
    )


def test_socket_ack_happens_only_after_durable_insert(tmp_path) -> None:
    asyncio.run(_socket_ack_happens_only_after_durable_insert(tmp_path))


async def _socket_ack_happens_only_after_durable_insert(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    websocket = _WebSocket(store)
    socket = SocketModeClient(
        _Slack(),
        store,
        bot_user_id="U_BOT",
    )

    await socket.handle_raw_message(websocket, _raw())

    assert websocket.sent == [{"envelope_id": "env-1"}]


def test_failed_durable_insert_is_not_acknowledged(tmp_path, monkeypatch) -> None:
    asyncio.run(_failed_durable_insert_is_not_acknowledged(tmp_path, monkeypatch))


async def _failed_durable_insert_is_not_acknowledged(tmp_path, monkeypatch) -> None:
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
        await socket.handle_raw_message(websocket, _raw())
    assert websocket.sent == []
