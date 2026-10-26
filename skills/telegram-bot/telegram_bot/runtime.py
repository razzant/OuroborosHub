"""Bounded Telegram provider lifecycle and presence Host integration."""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from .api import TelegramClient, TelegramApiError, _split_text
from .delivery import delivery_report
from .formatting import prepare_text, prepare_caption
from .custody import CustodyStore, InboxLease, OutboxLease
from .events import parse_telegram_update
from .host import PresenceHostHTTPError, PresenceSubmission, PresenceWorkResult


_MAX_OUTBOX_ATTEMPTS = 5


class PlaceHeld(RuntimeError):
    """Unsent custody waits for the current place policy, without a provider failure."""


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
        self._bot_username = ""
        self._bot_label = "not connected"
        self._runtime_state = "not_started"
        self._last_error = ""

    async def run(self) -> None:
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._client_lock = asyncio.Lock()
        self._run_task = asyncio.current_task()
        # Single owner per state dir and no worker of this runtime has started,
        # so any leased row is stale (see CustodyStore.release_own_leases).
        released = self.store.release_own_leases()
        if any(released.values()):
            self._log("info", f"telegram-bot: released stale leases {released}")
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
            "delivery_reporting": getattr(self.submitter, "delivery_reporting_status", "unsupported"),
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
                discover = getattr(self.submitter, "discover_delivery_support", None)
                if discover:
                    await discover()
                updates = await client.get_updates(
                    offset=self.store.telegram_offset(), timeout_sec=25
                )
                for update in updates:
                    update_id = _update_id(update)
                    if update_id is None:
                        continue
                    event = parse_telegram_update(
                        update,
                        bot_account_id=self._bot_id,
                        management_group_id=self._management_group_id(),
                        places=bool(getattr(self.submitter, "places_version", 0)),
                        bot_username=self._bot_username,
                    )
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

    def _management_group_id(self) -> str:
        try:
            settings = json.loads(
                (self.state_dir / "settings.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        return (
            str(settings.get("management_group_id") or "").strip()
            if isinstance(settings, dict)
            else ""
        )

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
            self._bot_username = username
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
            try:
                result = await self.submitter.submit(lease.payload, staged)
            except PresenceHostHTTPError as refusal:
                if not self._settle_refusal(lease, refusal):
                    raise
            else:
                self.store.record_submission(
                    lease.event_id,
                    binding_id=result.binding_id,
                    turn_ref=result.turn_ref,
                    outcome=result.outcome,
                    text=result.text,
                    work_ref=result.work_ref,
                    delivery_reporting_version=result.delivery_reporting_version,
                )
            self._remove_staged_files(staged)
            return True
        except asyncio.CancelledError:
            self.store.release_inbox(
                lease.event_id, reason="worker_cancelled", retry_after_sec=0
            )
            raise
        except Exception as exc:
            # Retry, blocked (repair first) and untyped Host errors, whatever their
            # HTTP status, keep the event first in its conversation with backoff.
            self.store.release_inbox(
                lease.event_id,
                reason=_error(exc),
                retry_after_sec=_retry_delay(lease.attempts),
            )
            self._last_error = _error(exc)
            return True

    def _settle_refusal(self, lease: InboxLease, refusal: PresenceHostHTTPError) -> bool:
        """Settle a typed final Host answer; False keeps the event for retry.

        ``retained`` hands the event to observation and releases its conversation;
        ``rejected`` ends this poison input. Both keep the complete receipt.
        """
        receipt = {"http_status": refusal.status_code, "response": refusal.payload}
        if refusal.disposition == "retained":
            self.store.record_retention(
                lease.event_id, binding_id=refusal.binding_id, receipt=receipt
            )
        elif refusal.disposition == "rejected":
            self.store.mark_inbox_failed(lease.event_id, reason=_error(refusal), receipt=receipt)
        else:
            return False
        self._last_error = _error(refusal)
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
            if result.outputs:
                self.store.record_work_outputs(lease.work_ref, result.outputs)
            if result.terminal:
                # A child first named by a terminal view is retained in the same commit
                # that settles this reference, so it is still observed after a restart.
                self.store.complete_work(
                    lease.work_ref,
                    status=result.status,
                    text=result.text if result.outcome in {"message", "deferred"} and not result.output_ref else "",
                    child_ref=result.child_work_ref,
                )
            elif result.status == "unresolved":
                # An older Host without a canonical answer yet: keep observing at the
                # ordinary backoff pace, retaining any admitted child it names. Never a verdict.
                self.store.release_work(
                    lease.work_ref,
                    reason=f"presence_work_unresolved: {result.code}",
                    retry_after_sec=_retry_delay(lease.attempts),
                    child_ref=result.child_work_ref,
                )
            else:
                self.store.release_work(
                    lease.work_ref,
                    reason="presence_work_pending",
                    retry_after_sec=5.0,
                    child_ref=result.child_work_ref,
                )
            return True
        except asyncio.CancelledError:
            self.store.release_work(
                lease.work_ref, reason="worker_cancelled", retry_after_sec=0
            )
            raise
        except Exception as exc:
            self.store.release_work(
                lease.work_ref,
                reason=_error(exc),
                retry_after_sec=_retry_delay(lease.attempts),
            )
            self._last_error = _error(exc)
            return True

    async def _outbox_worker(self) -> None:
        reporting = None
        try:
            while not self._stopping:
                if reporting is None or reporting.done():
                    if reporting is not None:
                        await reporting
                    reporting = asyncio.create_task(self.process_one_delivery_report())
                worked = await self.process_one_outbox()
                if not worked:
                    await self._pause(0.5)
        finally:
            if reporting is not None:
                reporting.cancel()
                await asyncio.gather(reporting, return_exceptions=True)

    async def process_one_delivery_report(self) -> bool:
        pending = self.store.next_delivery_report()
        if pending is None:
            return False
        delivery_id, index, payload = pending
        try:
            await self.submitter.report_delivery(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.finish_delivery_report(delivery_id, index, error=_error(exc))
        else:
            self.store.finish_delivery_report(delivery_id, index)
        return True

    async def process_one_outbox(self) -> bool:
        lease = self.store.claim_outbox(lease_sec=120.0)
        if lease is None:
            return False
        try:
            client = await self._ready_client()
            if client is None:
                raise RuntimeError("telegram token is unavailable")
            reporting = dict(lease.payload.get("_reporting") or {"version": 0})
            if reporting.get("version") == -1:
                discover = getattr(self.submitter, "discover_delivery_support", None)
                reporting["version"] = await discover() if discover else 0
                reporting["status"] = getattr(self.submitter, "delivery_reporting_status", "unsupported")
            if reporting.get("version") == 1 and not reporting.get("account_id"):
                reporting["account_id"] = self._bot_id
            lease = replace(lease, payload={**lease.payload, "_reporting": reporting})
            self.store.checkpoint_outbox(lease.delivery_id, lease.payload)
            async def before_send():
                check = getattr(self.submitter, "place_hold", None)
                if check:
                    reason = await check(self._bot_id, str(lease.payload.get("chat_id") or ""))
                    if reason:
                        raise PlaceHeld(reason)

            receipt = await _deliver(client, lease, self.store, before_send=before_send)
            self.store.mark_delivered(lease.delivery_id, provider_receipt=receipt)
            return True
        except PlaceHeld as exc:
            self.store.hold_outbox(lease.delivery_id, str(exc))
            return True
        except asyncio.CancelledError:
            self.store.release_outbox(
                lease.delivery_id, reason="worker_cancelled", retry_after_sec=0
            )
            raise
        except Exception as exc:
            if (lease.payload.get("kind") == "operation" and isinstance(exc, TelegramApiError)
                    and exc.error_code == 0):
                # The provider may have accepted this mutation before the
                # response was lost. Do not execute a second physical edit or
                # reaction merely because this lease can be reclaimed.
                payload = self.store.outbox_payload(lease.delivery_id)
                report = delivery_report(lease.delivery_id, payload, part_id="status",
                                         state="uncertain", error=_error(exc))
                self.store.mark_outbox_failed(lease.delivery_id, reason=_error(exc),
                                              report=report, state="uncertain")
                self._last_error = _error(exc)
                return True
            if lease.attempts >= _MAX_OUTBOX_ATTEMPTS:
                payload = self.store.outbox_payload(lease.delivery_id)
                state = "failed" if isinstance(exc, TelegramApiError) and exc.error_code else "uncertain"
                report = delivery_report(lease.delivery_id, payload, part_id="status", state=state, error=_error(exc))
                self.store.mark_outbox_failed(lease.delivery_id, reason=_error(exc), report=report)
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


async def _deliver(
    client: TelegramClient, lease: OutboxLease, store: CustodyStore, *, before_send=None,
) -> Dict[str, Any]:
    payload = lease.payload
    kind = str(payload.get("kind") or "message")
    chat_id = str(payload.get("chat_id") or "").strip()
    if not chat_id:
        raise ValueError("outbox chat_id is required")
    topic_id = _optional_int(payload.get("topic_id"))
    reply_id = _optional_int(payload.get("reply_to_message_id"))
    if kind == "message":
        messages = list(payload.get("_sent_messages") or [])
        chunks = payload.get("_rendered_chunks")
        if chunks is None:
            # Rows from before formatting retain literal presentation. If some
            # chunks were already delivered, retain the exact old split too.
            chunks = (
                [
                    {"text": chunk, "parse_mode": ""}
                    for chunk in _split_text(str(payload.get("text") or ""), 4000)
                ]
                if messages
                else prepare_text(
                    str(payload.get("text") or ""),
                    markdown=payload.get("markdown", False),
                )
            )
            payload = {**payload, "_rendered_chunks": chunks}
            store.checkpoint_outbox(lease.delivery_id, payload)
        for index in range(len(messages), len(chunks)):
            chunk = chunks[index]
            if before_send:
                await before_send()
            result = await client.send_text_chunk(
                chat_id,
                chunk["text"],
                parse_mode=chunk["parse_mode"],
                topic_id=topic_id,
                reply_to_message_id=reply_id if index == 0 else None,
            )
            report = delivery_report(lease.delivery_id, payload, part_id=index, state="delivered", receipt=result,
                                     text=chunk["text"], fmt="html" if chunk["parse_mode"] == "HTML" else "plain")
            messages.append({key: value for key, value in result.items() if key != "_delivery"})
            store.checkpoint_outbox(
                lease.delivery_id, {**payload, "_sent_messages": messages}, report=report
            )
        return {"kind": kind, "messages": messages}
    if kind == "moderation":
        if before_send:
            await before_send()
        result = await client.moderate(
            str(payload["action"]), dict(payload["parameters"])
        )
        return {"kind": kind, "action": payload["action"], "result": result}
    if kind == "operation":
        method = str(payload.get("method") or "")
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("Telegram operation parameters are required")
        if "_operation_result" in payload:
            return {"kind": kind, "method": method, "result": payload["_operation_result"]}
        if before_send:
            await before_send()
        result = await client.operation(method, dict(parameters))
        report = delivery_report(
            lease.delivery_id, payload, part_id="0", state="delivered",
            receipt=result, text=str(payload.get("text") or ""), fmt="plain",
        )
        store.checkpoint_outbox(
            lease.delivery_id,
            {**payload, "_operation_result": result},
            report=report,
        )
        return {"kind": kind, "method": method, "result": result}
    if kind not in {"photo", "document"}:
        raise ValueError(f"unsupported Telegram outbox kind: {kind}")
    if payload.get("_sent_media"):
        return {"kind": kind, "message": payload["_sent_media"]}
    caption = payload.get("_rendered_caption")
    if caption is None:
        caption = prepare_caption(
            str(payload.get("caption") or ""), markdown=payload.get("markdown", False)
        )
        store.checkpoint_outbox(
            lease.delivery_id, {**payload, "_rendered_caption": caption}
        )
    if before_send:
        await before_send()
    result = await client.send_media(
        kind,
        chat_id,
        pathlib.Path(str(payload.get("file_path") or "")),
        prepared_caption=caption,
        topic_id=topic_id,
        reply_to_message_id=reply_id,
    )
    report = delivery_report(lease.delivery_id, payload, part_id=0, state="delivered", receipt=result,
                             text=caption["text"], fmt="html" if caption["parse_mode"] == "HTML" else "plain")
    result = {key: value for key, value in result.items() if key != "_delivery"}
    store.checkpoint_outbox(lease.delivery_id, {**payload, "_sent_media": result}, report=report)
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
