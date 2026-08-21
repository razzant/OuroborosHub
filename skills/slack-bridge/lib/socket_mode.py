from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import websockets

from .events import parse_socket_envelope
from .slack_api import SlackApiError, SlackClient
from .store import BridgeStore

log = logging.getLogger(__name__)

_FATAL_AUTH_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "missing_scope",
        "invalid_app_token",
        "invalid_token",
        "not_allowed_token_type",
    }
)


class _ReconnectRequested(RuntimeError):
    pass


class SocketModeClient:
    """Slack Socket Mode receiver with durable-before-ACK custody."""

    def __init__(
        self,
        slack: SlackClient,
        store: BridgeStore,
        *,
        bot_user_id: str,
        connector: Callable[..., Any] = websockets.connect,
    ) -> None:
        self.slack = slack
        self.store = store
        self.bot_user_id = str(bot_user_id)
        self._connector = connector
        self._stop = asyncio.Event()
        self._websocket: Any = None

    async def close(self) -> None:
        self._stop.set()
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                log.debug("Slack websocket close failed", exc_info=True)

    async def handle_raw_message(
        self, websocket: Any, raw_message: str | bytes
    ) -> None:
        """Persist one envelope, then and only then send its Socket ACK."""

        try:
            payload = json.loads(raw_message)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Slack Socket Mode sent invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Slack Socket Mode envelope must be an object")

        envelope_id = str(payload.get("envelope_id") or "").strip()
        message_type = str(payload.get("type") or "").strip()
        if envelope_id:
            parsed = parse_socket_envelope(
                payload,
                bot_user_id=self.bot_user_id,
            )
            row_id, inserted = self.store.ingest_envelope(payload, parsed)
            await websocket.send(json.dumps({"envelope_id": envelope_id}))
            self.store.set_runtime(
                last_event_at=time.time(),
                last_inbox_row_id=row_id,
                last_event_was_new=inserted,
            )

        if message_type == "disconnect":
            raise _ReconnectRequested(str(payload.get("reason") or "disconnect"))

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self.store.set_runtime(socket_state="connecting", last_socket_error="")
            try:
                socket_url = await self.slack.open_socket_url()
                async with self._connector(
                    socket_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024,
                ) as websocket:
                    self._websocket = websocket
                    self.store.set_runtime(
                        socket_state="connected",
                        connected_at=time.time(),
                        last_socket_error="",
                    )
                    backoff = 1.0
                    while not self._stop.is_set():
                        raw_message = await websocket.recv()
                        if raw_message:
                            await self.handle_raw_message(websocket, raw_message)
            except asyncio.CancelledError:
                raise
            except _ReconnectRequested as exc:
                self.store.set_runtime(
                    socket_state="reconnecting", last_socket_error=str(exc)
                )
            except SlackApiError as exc:
                self.store.set_runtime(
                    socket_state="error", last_socket_error=exc.error
                )
                if exc.error in _FATAL_AUTH_ERRORS:
                    raise
                log.warning("Slack Socket Mode API error: %s", exc)
            except websockets.exceptions.ConnectionClosed as exc:
                self.store.set_runtime(
                    socket_state="reconnecting",
                    last_socket_error=f"connection_closed:{exc.code}",
                )
            except Exception as exc:
                self.store.set_runtime(
                    socket_state="error", last_socket_error=str(exc)[:500]
                )
                log.warning("Slack Socket Mode connection failed: %s", exc)
            finally:
                self._websocket = None

            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, 30.0)
        self.store.set_runtime(socket_state="disconnected")
