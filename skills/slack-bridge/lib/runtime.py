from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import pathlib
import re
from typing import Any

from .host_adapter import HostBindingTerminalError, PresenceHostAdapter
from .slack_api import SlackApiError, SlackClient, chunk_message
from .socket_mode import SocketModeClient
from .store import BridgeStore, InboxItem

log = logging.getLogger(__name__)
_MAX_OUTBOX_ATTEMPTS = 5


def _event_directory_name(item: InboxItem) -> str:
    source = item.event_id or item.envelope_id or str(item.row_id)
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", source)
    return clean[:120] or str(item.row_id)


class InboundWorker:
    def __init__(
        self,
        store: BridgeStore,
        slack: SlackClient,
        host: PresenceHostAdapter,
        *,
        staged_root: pathlib.Path,
    ) -> None:
        self.store = store
        self.slack = slack
        self.host = host
        self.staged_root = staged_root

    async def process_once(self) -> bool:
        item = self.store.claim_inbox(lease_seconds=90.0)
        if item is None:
            return False
        try:
            if item.files and not item.staged_files:
                staged = await self.slack.stage_private_files(
                    item.files,
                    destination=self.staged_root / _event_directory_name(item),
                )
                staged_dicts = tuple(file.as_dict() for file in staged)
                self.store.set_staged_files(item.row_id, item.lease_token, staged_dicts)
                item = dataclasses.replace(item, staged_files=staged_dicts)

            reference = item.host_reference
            if not reference:
                reference = str(await self.host.submit(item)).strip()
                if not reference:
                    raise RuntimeError(
                        "Presence Host adapter returned an empty reference"
                    )
                self.store.set_host_reference(item.row_id, item.lease_token, reference)

            status = await self.host.status(reference)
            delivery_key = hashlib.sha256(reference.encode("utf-8")).hexdigest()
            for index, text in enumerate(status.texts):
                chunks = chunk_message(text)
                if not chunks:
                    continue
                self.store.enqueue_outbox(
                    request_id=f"presence:{delivery_key}:ack:{index}",
                    target=item.channel_id,
                    thread_ts=item.reply_thread_ts,
                    chunks=chunks,
                )
            if status.state == "failed":
                self.store.fail_inbox(
                    item.row_id,
                    item.lease_token,
                    status.error or "Presence Host turn failed",
                )
                return True
            if status.state != "ready":
                self.store.retry_inbox(
                    item.row_id,
                    item.lease_token,
                    f"Host turn state: {status.state}",
                    delay_seconds=5.0,
                )
                return True

            delivery = await self.host.deliver(reference)
            for index, text in enumerate(delivery.texts):
                chunks = chunk_message(text)
                if not chunks:
                    continue
                self.store.enqueue_outbox(
                    request_id=f"presence:{delivery_key}:final:{index}",
                    target=item.channel_id,
                    thread_ts=item.reply_thread_ts,
                    chunks=chunks,
                )
            self.store.complete_inbox(item.row_id, item.lease_token)
            return True
        except asyncio.CancelledError:
            raise
        except HostBindingTerminalError as exc:
            self.store.fail_inbox(item.row_id, item.lease_token, str(exc))
            log.warning(
                "Slack inbound event %s failed terminally: %s", item.row_id, exc
            )
            return True
        except Exception as exc:
            delay = min(60.0, 2.0 ** min(item.attempts, 5))
            self.store.retry_inbox(
                item.row_id,
                item.lease_token,
                str(exc),
                delay_seconds=delay,
            )
            log.warning("Slack inbound event %s will retry: %s", item.row_id, exc)
            return True


class OutboundWorker:
    def __init__(self, store: BridgeStore, slack: SlackClient) -> None:
        self.store = store
        self.slack = slack

    async def process_once(self) -> bool:
        item = self.store.claim_outbox(lease_seconds=60.0)
        if item is None:
            return False
        try:
            channel = await self.slack.resolve_target(item.target)
            result = await self.slack.post_message(
                channel=channel,
                text=item.text,
                thread_ts=item.thread_ts,
            )
            self.store.complete_outbox(
                item.row_id,
                item.lease_token,
                provider_message_ts=str(result.get("ts") or ""),
            )
            return True
        except asyncio.CancelledError:
            raise
        except SlackApiError as exc:
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error)
                log.warning(
                    "Slack outbox item %s failed after %s attempts: %s",
                    item.row_id,
                    item.attempts,
                    exc,
                )
                return True
            delay = exc.retry_after or min(60.0, 2.0 ** min(item.attempts, 5))
            self.store.retry_outbox(
                item.row_id,
                item.lease_token,
                exc.error,
                delay_seconds=delay,
            )
            log.warning("Slack outbox item %s will retry: %s", item.row_id, exc)
            return True
        except Exception as exc:
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc))
                log.warning(
                    "Slack outbox item %s failed after %s attempts: %s",
                    item.row_id,
                    item.attempts,
                    exc,
                )
                return True
            delay = min(60.0, 2.0 ** min(item.attempts, 5))
            self.store.retry_outbox(
                item.row_id,
                item.lease_token,
                str(exc),
                delay_seconds=delay,
            )
            log.warning("Slack outbox item %s will retry: %s", item.row_id, exc)
            return True


async def _worker_loop(worker: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        worked = await worker.process_once()
        if worked:
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass


class BridgeRuntime:
    def __init__(
        self,
        *,
        store: BridgeStore,
        slack: SlackClient,
        host: PresenceHostAdapter,
        bot_user_id: str,
        inbound_workers: int = 4,
        outbound_workers: int = 2,
    ) -> None:
        self.store = store
        self.slack = slack
        self.host = host
        self.socket = SocketModeClient(
            slack,
            store,
            bot_user_id=bot_user_id,
        )
        self.inbound_workers = max(1, min(16, int(inbound_workers)))
        self.outbound_workers = max(1, min(8, int(outbound_workers)))
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    async def run(self) -> None:
        inbound_state = "active" if self.host.available else "missing_binding_id"
        self.store.set_runtime(host_adapter_state=inbound_state)
        self._tasks = [asyncio.create_task(self.socket.run(), name="slack-socket-mode")]
        for index in range(self.outbound_workers):
            worker = OutboundWorker(self.store, self.slack)
            self._tasks.append(
                asyncio.create_task(
                    _worker_loop(worker, self._stop),
                    name=f"slack-outbound-{index}",
                )
            )
        if self.host.available:
            staged_root = self.store.state_dir / "staged"
            for index in range(self.inbound_workers):
                worker = InboundWorker(
                    self.store,
                    self.slack,
                    self.host,
                    staged_root=staged_root,
                )
                self._tasks.append(
                    asyncio.create_task(
                        _worker_loop(worker, self._stop),
                        name=f"slack-inbound-{index}",
                    )
                )
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.close()

    async def close(self) -> None:
        self._stop.set()
        await self.socket.close()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(
                *(task for task in self._tasks if task is not current),
                return_exceptions=True,
            )
        self._tasks = []
        close_host = getattr(self.host, "aclose", None)
        if close_host is not None:
            await close_host()
        await self.slack.aclose()
        self.store.set_runtime(socket_state="disconnected")
