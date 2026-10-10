from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import pathlib
import re

import httpx
from typing import Any, Sequence

from .host_adapter import HostBindingTerminalError, HostOutput, HostTurnStatus, PresenceHostAdapter
from .provider_context import capture_context
from .slack_api import SlackApiError, SlackClient, SlackConfigurationError, SlackMutationUncertain, chunk_message, mutation_may_have_applied
from .socket_mode import SocketModeClient
from .store import BridgeStore, InboxItem, InboxLeaseLost, OutboxItem

log = logging.getLogger(__name__)
_MAX_OUTBOX_ATTEMPTS = 5
# Budget the Host's 1800-second initial request plus a recovery buffer, and
# refresh once before its finite continuation poll. Staging or suspension can
# still outlast this budget; the token checks remain the ownership authority.
_INBOUND_LEASE_SECONDS = 2100.0


def _event_directory_name(item: InboxItem) -> str:
    source = item.event_id or item.envelope_id or str(item.row_id)
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", source)
    return clean[:120] or str(item.row_id)


async def _discover_reporting(host: Any, store: BridgeStore) -> int:
    discover = getattr(host, "discover_delivery_support", None)
    mode = await discover() if discover is not None else 0
    store.set_runtime(
        presence_delivery_version=mode,
        presence_continuation_version=getattr(host, "continuation_version", 0),
        history_reporting_state=getattr(host, "delivery_reporting_status", "unsupported"),
        history_reporting_limitation="" if mode else "Host delivery reporting unavailable; provider sending remains enabled.",
    )
    return mode


def _automatic_origin(item: InboxItem, turn_ref: str) -> dict[str, str]:
    origin = {"kind": "automatic", "source_event_id": item.provider_event_key}
    if turn_ref:
        origin["task_id"] = turn_ref
    return origin


def _output_request_id(item: InboxItem, output: HostOutput) -> str:
    """One outbox identity per Host output and destination, never per text or turn index."""
    key = json.dumps([output.identity, item.team_id, item.channel_id, item.reply_thread_ts],
                     ensure_ascii=False, separators=(",", ":"))
    return f"presence-output:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _delivery_report(item: Any, state: str, *, result: dict[str, Any] | None = None,
                     error: str = "") -> dict[str, Any] | None:
    return item.delivery_report(state, result=result, error=error)


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

    def _enqueue_outputs(self, item: InboxItem, outputs: Sequence[HostOutput], mode: int) -> None:
        for output in outputs:
            chunks = chunk_message(output.text)
            if not chunks:
                continue
            self.store.enqueue_outbox(
                request_id=_output_request_id(item, output),
                target=item.channel_id,
                thread_ts=item.reply_thread_ts,
                chunks=chunks,
                origin=_automatic_origin(item, output.turn_ref),
                delivery_reporting_version=mode,
                output_ref=output.output_ref,
            )

    def _enqueue_status(self, item: InboxItem, reference: str, status: HostTurnStatus) -> None:
        delivery_key = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        for index, text in enumerate(status.texts):
            chunks = chunk_message(text)
            if chunks:
                self.store.enqueue_outbox(
                    request_id=f"presence:{delivery_key}:ack:{index}",
                    target=item.channel_id, thread_ts=item.reply_thread_ts, chunks=chunks,
                    origin=_automatic_origin(item, status.turn_ref),
                    delivery_reporting_version=status.delivery_reporting_version,
                )
        self._enqueue_outputs(item, status.outputs, status.delivery_reporting_version)

    async def process_once(self) -> bool:
        item = self.store.claim_inbox(lease_seconds=_INBOUND_LEASE_SECONDS)
        if item is None:
            return False
        try:
            return await self._process_claimed(item)
        except InboxLeaseLost:
            # Also covers loss during the retry/failure handlers below. Only
            # the new owner may checkpoint this row; never retry its old token.
            log.info("Slack inbound event %s lease lost; ending obsolete attempt", item.row_id)
            return True

    async def _process_claimed(self, item: InboxItem) -> bool:
        try:
            if not item.host_reference and item.provider_context is None:
                snapshot = await capture_context(self.slack, item, self.store.workspace_name())
                self.store.set_provider_context(item.row_id, item.lease_token, snapshot)
                item = dataclasses.replace(item, provider_context=snapshot)
            if item.files and not item.staged_files and not item.host_reference:
                # One outcome per declared file is committed before submit, so a
                # refused file never repeats and a lost Host reply resubmits the
                # same event. A retryable failure raises and commits nothing.
                staged = await self.slack.stage_inbound_files(
                    item.files,
                    destination=self.staged_root / _event_directory_name(item),
                )
                self.store.set_staged_files(item.row_id, item.lease_token, staged)
                item = dataclasses.replace(item, staged_files=staged)

            reference = item.host_reference
            if not reference:
                await _discover_reporting(self.host, self.store)
                if item.transport_queue is None:
                    # What this conversation still had queued at the first attempt; a
                    # retry resubmits the same observation, never a silently newer one.
                    queue = self.store.conversation_queue(item)
                    self.store.set_transport_queue(item.row_id, item.lease_token, queue)
                    item = dataclasses.replace(item, transport_queue=queue)
                reference = str(await self.host.submit(item)).strip()
                if not reference:
                    raise RuntimeError(
                        "Presence Host adapter returned an empty reference"
                    )
                self.store.set_host_reference(item.row_id, item.lease_token, reference)
                if reference.startswith(("continuing:", "refused:")):
                    # The durable reference separates submit from the new finite
                    # queue-report/poll phase. Later attempts already claim fresh
                    # leases. This is a token-checked renewal, not a heartbeat.
                    self.store.renew_inbox_lease(
                        item.row_id, item.lease_token, lease_seconds=_INBOUND_LEASE_SECONDS
                    )

            updates_factory = getattr(self.host, "status_updates", None)
            if updates_factory is None:
                status = await self.host.status(reference)
                self._enqueue_status(item, reference, status)
            else:
                snapshot = None
                if reference.startswith("continuing:") and hasattr(self.host, "refresh_transport_queue"):
                    snapshot = item.transport_queue_report
                    if snapshot is None:
                        snapshot = self.store.conversation_queue(item)
                        self.store.set_transport_queue_report(item.row_id, item.lease_token, snapshot)
                updates = (updates_factory(reference, transport_queue=snapshot) if snapshot is not None
                           else updates_factory(reference))
                try:
                    async for status in updates:
                        if status.host_reference and status.host_reference != reference:
                            self.store.set_host_reference(item.row_id, item.lease_token, status.host_reference)
                            reference = status.host_reference
                        # Commit each available selection before advancing either
                        # network poll; outbound workers may send it immediately.
                        self._enqueue_status(item, reference, status)
                        if snapshot is not None and status.transport_queue_recorded:
                            self.store.set_transport_queue_report(item.row_id, item.lease_token, None)
                            snapshot = None
                finally:
                    await updates.aclose()
            delivery_key = hashlib.sha256(reference.encode("utf-8")).hexdigest()
            if status.state == "failed":
                self.store.fail_inbox(
                    item.row_id,
                    item.lease_token,
                    status.error or "Presence Host turn failed",
                )
                return True
            if status.state != "ready":
                # A poll that could not be read backs off; an ordinary pending one keeps polling.
                self.store.retry_inbox(
                    item.row_id,
                    item.lease_token,
                    status.error or f"Host turn state: {status.state}",
                    delay_seconds=min(60.0, 2.0 ** min(item.attempts, 5)) if status.error else 5.0,
                )
                return True

            delivery = await self.host.deliver(reference)
            self._enqueue_outputs(item, getattr(delivery, "outputs", ()), delivery.delivery_reporting_version)
            for index, text in enumerate(delivery.texts):
                chunks = chunk_message(text)
                if not chunks:
                    continue
                self.store.enqueue_outbox(
                    request_id=f"presence:{delivery_key}:final:{index}",
                    target=item.channel_id,
                    thread_ts=item.reply_thread_ts,
                    chunks=chunks,
                    origin=_automatic_origin(item, delivery.turn_ref),
                    delivery_reporting_version=delivery.delivery_reporting_version,
                )
            self.store.complete_inbox(item.row_id, item.lease_token)
            return True
        except asyncio.CancelledError:
            raise
        except InboxLeaseLost:
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
    def __init__(self, store: BridgeStore, slack: SlackClient, host: Any = None) -> None:
        self.store = store
        self.slack = slack
        self.host = host
        self._report_task: asyncio.Task[None] | None = None

    async def _report(self, report: dict[str, Any]) -> None:
        try:
            await self.host.report_delivery(report["payload"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.finish_report(report, error=str(exc) or type(exc).__name__)
        else:
            self.store.finish_report(report)

    def _advance_reporting(self) -> bool:
        """At most one callback per existing worker, never awaited by sending."""
        advanced = False
        if self._report_task is not None:
            if not self._report_task.done():
                return False
            try:
                self._report_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warning("Slack history report checkpoint failed: %s", type(exc).__name__)
            self._report_task = None
            advanced = True
        if self.host is not None:
            report = self.store.claim_report()
            if report is not None:
                self._report_task = asyncio.create_task(self._report(report), name="slack-delivery-report")
                advanced = True
        return advanced

    async def aclose(self) -> None:
        task, self._report_task = self._report_task, None
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _cancel_started_send(self, item: OutboxItem) -> None:
        try:
            self.store.fail_outbox(
                item.row_id, item.lease_token, "send_cancelled_after_dispatch", state="uncertain",
                result={"uncertain": True},
                report_payload=_delivery_report(item, "uncertain", error="send_cancelled_after_dispatch")
                if item.kind == "text" else None,
            )
        except Exception:
            # The durable attempt marker still prevents a resend if checkpointing
            # fails, or another worker has already recovered this expired lease.
            log.exception("Slack cancelled send %s could not checkpoint uncertainty", item.row_id)

    async def process_once(self) -> bool:
        # A slow callback keeps its own task while this worker continues sending.
        reported = self._advance_reporting()
        item = self.store.claim_outbox(lease_seconds=120.0)
        if item is None:
            return reported
        item = dataclasses.replace(item, provider_account_id=(
            item.provider_account_id or str(self.store.runtime_value("workspace_id", ""))
        ))
        if item.kind == "mutation":
            return await self._process_mutation(item) or reported
        send_started = False
        try:
            channel = item.resolved_channel or await self.slack.resolve_target(item.target)
            self.store.set_resolved_target(item, channel, item.provider_account_id)
            item = dataclasses.replace(item, resolved_channel=channel)
            self.store.begin_send(item)
            send_started = True
            result = await self.slack.post_message(
                channel=channel,
                text=item.text,
                thread_ts=item.thread_ts,
                text_format=item.text_format,
            )
            actual_channel = str(result.get("channel") or channel)
            if actual_channel != channel:
                self.store.set_resolved_target(item, actual_channel, item.provider_account_id)
                item = dataclasses.replace(item, resolved_channel=actual_channel)
            self.store.complete_outbox(
                item.row_id,
                item.lease_token,
                provider_message_ts=str(result.get("ts") or ""),
                report_payload=_delivery_report(item, "delivered", result=result),
            )
            return True

        except asyncio.CancelledError:
            if send_started:
                self._cancel_started_send(item)
            raise
        except SlackApiError as exc:
            uncertain = send_started and mutation_may_have_applied(exc)
            if uncertain or item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                state = "uncertain" if uncertain else "failed"
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state=state,
                                       report_payload=_delivery_report(item, state, error=exc.error))
                log.warning("Slack outbox item %s ended %s after %s attempts: %s",
                            item.row_id, state, item.attempts, exc)
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
            before_request = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            uncertain = send_started and not before_request
            if uncertain or item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                state = "uncertain" if uncertain else "failed"
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state=state,
                                       report_payload=_delivery_report(item, state, error=type(exc).__name__))
                log.warning("Slack outbox item %s ended %s after %s attempts: %s",
                            item.row_id, state, item.attempts, exc)
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

    async def _process_mutation(self, item: OutboxItem) -> bool:
        payload = item.payload
        artifact = pathlib.Path(str(payload.get("path") or "")) if item.operation == "upload_file" else None
        def cleanup_artifact() -> None:
            if artifact is not None:
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    log.warning("Slack staged mutation artifact cleanup failed: %s", artifact)
        send_started = False
        try:
            operation = item.operation
            # Whole-upload custody is intentionally conservative: a crash may
            # leave unshared staged bytes, but never permits an automatic resend.
            # This is effect based, including a generic write made with GET.
            self.store.begin_send(item)
            send_started = True
            if operation == "upload_file":
                result = await self.slack.upload_file(
                    path=pathlib.Path(str(payload.get("path") or "")),
                    filename=str(payload.get("filename") or "upload"),
                    title=str(payload.get("title") or ""),
                    channel=str(payload.get("channel") or ""),
                    thread_ts=str(payload.get("thread_ts") or ""),
                    initial_comment=str(payload.get("initial_comment") or ""),
                )
            elif operation == "update_message":
                result = await self.slack.update_message(**payload)
            elif operation == "delete_message":
                result = await self.slack.delete_message(**payload)
            elif operation == "reaction_add":
                result = await self.slack.reaction(**payload, add=True)
            elif operation == "reaction_remove":
                result = await self.slack.reaction(**payload, add=False)
            elif operation == "pin_add":
                result = await self.slack.pin(**payload, add=True)
            elif operation == "pin_remove":
                result = await self.slack.pin(**payload, add=False)
            elif operation == "bookmark_add":
                result = await self.slack.bookmark(**payload, add=True)
            elif operation == "bookmark_remove":
                result = await self.slack.bookmark(**payload, add=False)
            elif operation == "join_conversation":
                result = await self.slack.join_conversation(str(payload.get("channel_id") or ""))
            elif operation == "generic_api":
                result = await self.slack.generic_request(
                    method=str(payload.get("method") or "POST"),
                    path=str(payload.get("path") or ""),
                    params=payload.get("params") if isinstance(payload.get("params"), dict) else {},
                    body=payload.get("body") if isinstance(payload.get("body"), dict) else {},
                    effect="write",
                )
            else:
                raise SlackApiError("unsupported_mutation")
            report = None
            provider_message = result.get("message") if isinstance(result, dict) else None
            if (operation == "generic_api" and isinstance(provider_message, dict)
                    and (payload.get("result_kind") == "message" or
                         (payload.get("result_kind", "auto") == "auto" and payload.get("path") == "chat.postMessage"))):
                # A provider-returned message plus exact channel/ts proves this
                # write produced speech; other generic effects remain operation
                # facts and never become invented spoken history.
                channel = str(result.get("channel") or "")
                timestamp = str(result.get("ts") or provider_message.get("ts") or "")
                message_text = provider_message.get("text")
                if channel and timestamp and isinstance(message_text, str):
                    self.store.set_resolved_target(item, channel, item.provider_account_id)
                    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
                    text_format = ("markdown" if "markdown_text" in body else
                                   "plain" if body.get("mrkdwn") is False else "mrkdwn")
                    item = dataclasses.replace(item, resolved_channel=channel, text=message_text,
                                               text_format=text_format)
                    report = _delivery_report(item, "delivered", result=result)
                    if report:
                        report["message"].update(operation=payload.get("path"), provider_message_id=timestamp)
            self.store.complete_outbox(item.row_id, item.lease_token,
                                       provider_message_ts=str(result.get("ts") or "") if isinstance(result, dict) else "",
                                       report_payload=report, result=result)
            cleanup_artifact()
            return True
        except asyncio.CancelledError:
            if send_started:
                self._cancel_started_send(item)
                cleanup_artifact()
            raise
        except SlackConfigurationError as exc:
            # Mutation arguments/input were rejected before any provider request,
            # despite the conservative marker already being persisted.
            self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state="failed",
                                   result={"uncertain": False})
            cleanup_artifact()
            return True
        except SlackMutationUncertain as exc:
            self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="uncertain", result={"uncertain": True})
            cleanup_artifact()
            log.warning("Slack mutation %s is uncertain after provider acceptance boundary: %s", item.request_id, exc)
            return True
        except SlackApiError as exc:
            if exc.status_code == 429 or exc.error == "ratelimited":
                if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                    self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="failed",
                                           result={"uncertain": False})
                    cleanup_artifact()
                else:
                    self.store.retry_outbox(item.row_id, item.lease_token, exc.error,
                                            delay_seconds=exc.retry_after or min(60.0, 2.0 ** min(item.attempts, 5)))
                return True
            provider_may_have_applied = send_started and mutation_may_have_applied(exc)
            if provider_may_have_applied:
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="uncertain",
                                       result={"uncertain": True})
                cleanup_artifact()
                return True
            if item.operation == "generic_api" or exc.error in {
                "already_reacted", "message_not_found", "cant_update_message", "missing_scope",
                "not_in_channel", "channel_not_found", "invalid_arguments", "invalid_auth",
                "not_allowed_token_type", "file_not_found", "invalid_channel",
                # conversations.join refusals that no retry can change.
                "already_in_channel", "is_archived", "method_not_supported_for_channel_type",
            }:
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="failed",
                                       result={"uncertain": False})
                cleanup_artifact()
                return True
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="failed",
                                       result={"uncertain": False})
                cleanup_artifact()
                return True
            self.store.retry_outbox(item.row_id, item.lease_token, exc.error, delay_seconds=exc.retry_after or min(60.0, 2.0 ** min(item.attempts, 5)))
            return True
        except Exception as exc:
            before_request = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            if send_started and not before_request:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state="uncertain",
                                       result={"uncertain": True})
                cleanup_artifact()
                return True
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state="failed", result={"uncertain": False})
                cleanup_artifact()
                return True
            self.store.retry_outbox(item.row_id, item.lease_token, str(exc), delay_seconds=min(60.0, 2.0 ** min(item.attempts, 5)))
            return True


async def _worker_loop(worker: Any, stop: asyncio.Event) -> None:
    try:
        while not stop.is_set():
            worked = await worker.process_once()
            if worked:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
    finally:
        close = getattr(worker, "aclose", None)
        if close is not None:
            await close()


class BridgeRuntime:
    def __init__(
        self,
        *,
        store: BridgeStore,
        slack: SlackClient,
        host: PresenceHostAdapter,
        bot_user_id: str,
        bot_id: str = "",
        app_id: str = "",
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
            bot_id=bot_id,
            app_id=app_id,
        )
        self.inbound_workers = max(1, min(16, int(inbound_workers)))
        self.outbound_workers = max(1, min(8, int(outbound_workers)))
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    async def run(self) -> None:
        await _discover_reporting(self.host, self.store)
        inbound_state = "active" if self.host.available else "missing_binding_id"
        self.store.set_runtime(host_adapter_state=inbound_state)
        self._tasks = [asyncio.create_task(self.socket.run(), name="slack-socket-mode")]
        for index in range(self.outbound_workers):
            worker = OutboundWorker(self.store, self.slack, self.host)
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
