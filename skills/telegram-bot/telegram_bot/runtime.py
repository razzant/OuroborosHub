"""Bounded Telegram provider lifecycle and presence Host integration."""

from __future__ import annotations

import asyncio
import pathlib
import re
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from .api import TelegramClient
from .custody import CustodyStore, InboxLease, OutboxLease
from .events import parse_telegram_update
from .host import PresenceHostHTTPError, PresenceSubmission, PresenceWorkResult


_MAX_OUTBOX_ATTEMPTS = 5
_TERMINAL_BINDING_HTTP_STATUSES = {403, 404}


class PresenceSubmitter(Protocol):
    """The frozen provider-to-Host contract used by the runtime."""

    async def submit(
        self, event: Dict[str, Any], staged_files: Sequence[pathlib.Path]
    ) -> PresenceSubmission: ...

    async def poll(self, work_ref: str, binding_id: str) -> PresenceWorkResult: ...


class TelegramTransportRuntime:
    def __init__(
        self,
        *,
        state_dir: pathlib.Path,
        token_provider: Callable[[], str],
        logger: Any,
        submitter: PresenceSubmitter,
        client_factory: Callable[[str], TelegramClient] = TelegramClient,
        inbox_workers: int = 2,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.store = CustodyStore(self.state_dir / "custody.sqlite3")
        self.token_provider = token_provider
        self.logger = logger
        self.submitter = submitter
        self.client_factory = client_factory
        self.inbox_workers = max(1, min(8, int(inbox_workers)))
        self._stopping = False
        self._stop_event: Optional[asyncio.Event] = None
        self._client: Optional[TelegramClient] = None
        self._client_token = ""
        self._client_lock: Optional[asyncio.Lock] = None
        self._run_task: Optional[asyncio.Task[Any]] = None
        self._bot_id = ""
        self._bot_label = "not connected"
        self._runtime_state = "not_started"
        self._last_error = ""

    async def run(self) -> None:
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._client_lock = asyncio.Lock()
        self._run_task = asyncio.current_task()
        workers = [
            asyncio.create_task(
                self._inbox_worker(), name=f"telegram-presence-inbox-{index}"
            )
            for index in range(self.inbox_workers)
        ]
        workers.append(
            asyncio.create_task(self._work_worker(), name="telegram-presence-work")
        )
        workers.append(
            asyncio.create_task(self._outbox_worker(), name="telegram-presence-outbox")
        )
        try:
            await self._poll_loop()
        finally:
            self._stopping = True
            if self._stop_event is not None:
                self._stop_event.set()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._runtime_state = "stopped"
            self._run_task = None

    def stop(self) -> None:
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    def status_snapshot(self) -> Dict[str, Any]:
        custody = self.store.status_snapshot()
        return {
            "runtime_state": self._runtime_state,
            "bot_label": self._bot_label,
            "last_error": self._last_error,
            "last_event_at": custody.get("last_event_at", ""),
            "last_delivery_at": custody.get("last_delivery_at", ""),
        }

    async def _poll_loop(self) -> None:
        backoff = 2.0
        while not self._stopping:
            try:
                client = await self._ready_client()
                if client is None:
                    await self._pause(5.0)
                    continue
                self._runtime_state = "polling"
                updates = await client.get_updates(
                    offset=self.store.telegram_offset(), timeout_sec=25
                )
                for update in updates:
                    update_id = _update_id(update)
                    if update_id is None:
                        continue
                    event = parse_telegram_update(update, bot_account_id=self._bot_id)
                    self.store.commit_update(update_id, event)
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._runtime_state = "provider_error"
                self._last_error = _error(exc)
                self._log(
                    "warning", f"telegram-bot: provider poll failed: {self._last_error}"
                )
                await self._pause(backoff)
                backoff = min(60.0, backoff * 1.7)

    async def _ready_client(self) -> Optional[TelegramClient]:
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            token = str(self.token_provider() or "").strip()
            if not token:
                self._runtime_state = "waiting_for_token"
                self._bot_label = "not configured"
                self._client = None
                self._client_token = ""
                return None
            if self._client is not None and token == self._client_token:
                return self._client
            client = self.client_factory(token)
            await client.delete_webhook()
            identity = await client.get_me()
            self._bot_id = str(identity.get("id") or "unknown")
            username = str(identity.get("username") or "").strip()
            self._bot_label = f"@{username}" if username else self._bot_id
            self._client = client
            self._client_token = token
            self._last_error = ""
            return client

    async def _inbox_worker(self) -> None:
        while not self._stopping:
            worked = await self.process_one_inbox()
            if not worked:
                await self._pause(0.5)

    async def process_one_inbox(self) -> bool:
        # A Host turn can run for up to 30 minutes; keep one local claimant
        # through that bounded request so another worker cannot resubmit it.
        lease = self.store.claim_inbox(lease_sec=1860.0)
        if lease is None:
            return False
        staged: List[pathlib.Path] = []
        try:
            staged = await self._stage_attachments(lease)
            result = await self.submitter.submit(lease.payload, staged)
            self.store.record_submission(
                lease.event_id,
                binding_id=result.binding_id,
                turn_ref=result.turn_ref,
                outcome=result.outcome,
                text=result.text,
                work_ref=result.work_ref,
            )
            self._remove_staged_files(staged)
            return True
        except asyncio.CancelledError:
            self.store.release_inbox(
                lease.event_id, reason="worker_cancelled", retry_after_sec=0
            )
            raise
        except Exception as exc:
            if (
                isinstance(exc, PresenceHostHTTPError)
                and exc.status_code in _TERMINAL_BINDING_HTTP_STATUSES
            ):
                self.store.mark_inbox_failed(lease.event_id, reason=_error(exc))
                self._remove_staged_files(staged)
            else:
                self.store.release_inbox(
                    lease.event_id,
                    reason=_error(exc),
                    retry_after_sec=_retry_delay(lease.attempts),
                )
            self._last_error = _error(exc)
            return True

    def _remove_staged_files(self, staged: Sequence[pathlib.Path]) -> None:
        for path in staged:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self._log(
                    "warning", f"telegram-bot: could not remove staged file {path.name}"
                )

    async def _stage_attachments(self, lease: InboxLease) -> List[pathlib.Path]:
        message = (
            lease.payload.get("message")
            if isinstance(lease.payload.get("message"), dict)
            else {}
        )
        attachments = message.get("attachments") or []
        if not attachments:
            return []
        client = await self._ready_client()
        if client is None:
            raise RuntimeError("telegram token is unavailable")
        event_dir = self.state_dir / "staging" / _safe_component(lease.event_id)
        staged = []
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict) or not attachment.get("file_id"):
                continue
            name = _safe_filename(
                str(attachment.get("file_name") or f"attachment-{index}")
            )
            destination = event_dir / f"{index}-{name}"
            if not destination.is_file() or destination.stat().st_size == 0:
                await client.download_file(str(attachment["file_id"]), destination)
            staged.append(destination)
        return staged

    async def _work_worker(self) -> None:
        while not self._stopping:
            worked = await self.process_one_work()
            if not worked:
                await self._pause(0.5)

    async def process_one_work(self) -> bool:
        lease = self.store.claim_work(lease_sec=120.0)
        if lease is None:
            return False
        try:
            result = await self.submitter.poll(lease.work_ref, lease.binding_id)
            if result.terminal:
                self.store.complete_work(
                    lease.event_id,
                    status=result.status,
                    text=result.text,
                )
            else:
                self.store.release_work(
                    lease.event_id,
                    reason="presence_work_pending",
                    retry_after_sec=5.0,
                )
            return True
        except asyncio.CancelledError:
            self.store.release_work(
                lease.event_id, reason="worker_cancelled", retry_after_sec=0
            )
            raise
        except Exception as exc:
            self.store.release_work(
                lease.event_id,
                reason=_error(exc),
                retry_after_sec=_retry_delay(lease.attempts),
            )
            self._last_error = _error(exc)
            return True

    async def _outbox_worker(self) -> None:
        while not self._stopping:
            worked = await self.process_one_outbox()
            if not worked:
                await self._pause(0.5)

    async def process_one_outbox(self) -> bool:
        lease = self.store.claim_outbox(lease_sec=120.0)
        if lease is None:
            return False
        try:
            client = await self._ready_client()
            if client is None:
                raise RuntimeError("telegram token is unavailable")
            receipt = await _deliver(client, lease)
            self.store.mark_delivered(lease.delivery_id, provider_receipt=receipt)
            return True
        except asyncio.CancelledError:
            self.store.release_outbox(
                lease.delivery_id, reason="worker_cancelled", retry_after_sec=0
            )
            raise
        except Exception as exc:
            if lease.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.mark_outbox_failed(lease.delivery_id, reason=_error(exc))
            else:
                self.store.release_outbox(
                    lease.delivery_id,
                    reason=_error(exc),
                    retry_after_sec=_retry_delay(lease.attempts),
                )
            self._last_error = _error(exc)
            return True

    async def _pause(self, seconds: float) -> None:
        if self._stop_event is None:
            await asyncio.sleep(max(0.01, seconds))
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.01, seconds))
        except asyncio.TimeoutError:
            return

    def _log(self, level: str, message: str) -> None:
        callback = getattr(self.logger, "log", None)
        if callable(callback):
            callback(level, message)


async def _deliver(client: TelegramClient, lease: OutboxLease) -> Dict[str, Any]:
    payload = lease.payload
    kind = str(payload.get("kind") or "message")
    chat_id = str(payload.get("chat_id") or "").strip()
    if not chat_id:
        raise ValueError("outbox chat_id is required")
    topic_id = _optional_int(payload.get("topic_id"))
    reply_id = _optional_int(payload.get("reply_to_message_id"))
    if kind == "message":
        messages = await client.send_message(
            chat_id,
            str(payload.get("text") or ""),
            topic_id=topic_id,
            reply_to_message_id=reply_id,
        )
        return {"kind": kind, "messages": messages}
    file_path = pathlib.Path(str(payload.get("file_path") or ""))
    caption = str(payload.get("caption") or "")
    if kind == "photo":
        result = await client.send_photo(
            chat_id,
            file_path,
            caption=caption,
            topic_id=topic_id,
            reply_to_message_id=reply_id,
        )
    elif kind == "document":
        result = await client.send_document(
            chat_id,
            file_path,
            caption=caption,
            topic_id=topic_id,
            reply_to_message_id=reply_id,
        )
    else:
        raise ValueError(f"unsupported Telegram outbox kind: {kind}")
    return {"kind": kind, "message": result}


def _update_id(update: Dict[str, Any]) -> Optional[int]:
    try:
        return int(update.get("update_id"))
    except (AttributeError, TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:160] or "event"


def _safe_filename(value: str) -> str:
    name = pathlib.PurePath(value.replace("\\", "/")).name
    clean = re.sub(r"[^A-Za-z0-9_. -]+", "_", name).strip(" .")
    return clean[:160] or "attachment"


def _retry_delay(attempts: int) -> float:
    return min(300.0, 2.0 ** min(max(1, attempts), 8))


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
