"""Bounded Telegram provider lifecycle and presence Host integration."""

from __future__ import annotations

import asyncio
import pathlib
import re
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from .api import TelegramClient, TelegramApiError, _split_text
from .delivery import (REACTION_HISTORY_UNAVAILABLE, REACTION_REPORT_UNREADABLE, delivery_report,
                       is_reaction_report, reaction_receipt, typed_reaction_report)
from .formatting import prepare_text, prepare_caption
from .custody import REACTION_WITHHELD, CustodyStore, InboxLease, OutboxLease, _chat_forum
from .discovery import group_sightings
from .events import parse_telegram_update
from .host import PresenceHostHTTPError, PresenceSubmission, PresenceWorkResult
from .settings import load_settings


_MAX_OUTBOX_ATTEMPTS = 5
_BOT_ID = re.compile(r"[1-9][0-9]*")
_REACTION_KINDS = frozenset({"message_reaction", "message_reaction_count"})
REACTION_METHOD = "setMessageReaction"
# Telegram's "the request is wrong" and "you may not" (https://core.telegram.org/api/errors; the
# Bot API documents no reaction-specific code and keeps its description as the explanation).
_REACTION_REFUSALS = frozenset({400, 403})
# Telegram answered that it did not do this now: bad token or address, or flood wait. Only these
# permit another call. Any other answer, such as a 408 or an unreadable one, says nothing about the effect.
_REACTION_NOT_DONE = frozenset({401, 404, 429})
_REACTION_MARKER = "_provider_call_started"
# Telegram's answer that this token names no bot: revoked or wrong (401), or malformed (404).
_TOKEN_REFUSED = frozenset({401, 404})


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
        # Telegram's yes that the store could not take yet, by delivery id. See _keep_known_reaction.
        self._unrecorded_reactions: Dict[str, Dict[str, Any]] = {}

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

    def confirmed_bot(self) -> Tuple[str, str]:
        """The bot id getMe confirmed for the token configured now, else ``""`` and why not.

        Local and without network. A token changed while getUpdates is held is
        another bot until getMe answers for it, so the earlier identity is not reused.
        """
        if self._run_task is None:
            return "", "Telegram bot not running"
        token = str(self.token_provider() or "").strip()
        if not token:
            return "", "Bot token missing"
        if token != self._client_token or not _BOT_ID.fullmatch(self._bot_id):
            return "", "Bot identity is unconfirmed; check the transport status"
        return self._bot_id, ""

    def recorded_bot(self) -> str:
        """The bot getMe last confirmed for the token configured now, whichever process's runtime asked.

        For tools, which also run in pooled workers whose runtime never starts. ``confirmed_bot``
        stays this runtime's own answer, which the status route reports as not running there.
        """
        return self.store.confirmed_account(str(self.token_provider() or "").strip())

    def status_snapshot(self, custody: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        custody = self.store.status_snapshot() if custody is None else custody
        return {
            "runtime_state": self._runtime_state,
            "bot_label": self._bot_label,
            **({"identity": self.submitter.connector_identity}
               if getattr(self.submitter, "presence_identity", 0) == 1
               and getattr(self.submitter, "connector_identity", {}) else {}),
            "last_error": self._last_error,
            "delivery_reporting": getattr(self.submitter, "delivery_reporting_status", "unsupported"),
            # 2: early custody answers; 0: each turn waits for its terminal (older Host).
            "presence_continuation_version": getattr(self.submitter, "continuation_version", 0),
            "reaction_context": ("available" if getattr(self.submitter, "presence_reaction_context", 0) == 1
                                 else "update_required"),
            "reaction_history": ("available" if getattr(self.submitter, "presence_explicit_completion", 0) == 1
                                 else "update_required"),
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
                # The bot this client answers for, even if another worker confirms a new token meanwhile.
                account, username = self._bot_id, self._bot_username
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
                    settings = load_settings(self.state_dir)
                    event = parse_telegram_update(
                        update,
                        bot_account_id=account,
                        management_group_id=str(settings.get("management_group_id") or "").strip(),
                        places=bool(getattr(self.submitter, "places_version", 0)),
                        bot_username=username,
                        words=settings.get("words", []),
                    )
                    # A membership or service update notes its group and moves the offset only.
                    self.store.commit_update(update_id, event, group_sightings(update, account))
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
            try:
                await client.delete_webhook()
                identity = await client.get_me()
            except TelegramApiError as exc:
                if exc.error_code in _TOKEN_REFUSED:
                    self.store.forget_confirmed_bot(token)
                raise
            bot_id = str(identity.get("id") or "unknown")
            # Before anything in memory changes: if the store refuses, the next call asks getMe again.
            if _BOT_ID.fullmatch(bot_id):
                self.store.record_confirmed_bot(token, bot_id)
            else:
                self.store.forget_confirmed_bot(token)
            self._bot_id = bot_id
            username = str(identity.get("username") or "").strip()
            self._bot_username = username
            self._bot_label = f"@{username}" if username else self._bot_id
            self.submitter.connector_identity = {"sends_as": {
                "id": self._bot_id, "label": self._bot_label, "kind": "bot"}}
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
        # A Host turn can run for up to 30 minutes (version 2 answers once its
        # author takes mail); keep one local claimant through that bounded
        # request so another worker cannot resubmit it.
        lease = self.store.claim_inbox(lease_sec=1860.0)
        if lease is None:
            return False
        if str((lease.payload.get("message") or {}).get("event_kind") or "") in _REACTION_KINDS:
            if not await self._reaction_deliverable(lease.payload):
                # Retained here as a terminal row, whatever a later Host can do.
                self.store.mark_inbox_failed(lease.event_id, reason=REACTION_WITHHELD)
                return True
        staged: List[pathlib.Path] = []
        try:
            staged = await self._stage_attachments(lease)
            try:
                result = await self.submitter.submit(lease.payload, staged)
            except PresenceHostHTTPError as refusal:
                delay = refusal.busy_retry_after
                if delay is not None:
                    # Nothing was admitted: the same event stays first in its
                    # conversation, with its staged files, and this worker moves on
                    # to other conversations until the Host's hint has passed.
                    self.store.defer_inbox(lease.event_id, reason=_error(refusal), retry_after_sec=delay)
                    return True
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
                    continuation_ref=result.continuation_ref,
                    output_ref=result.output_ref,
                    reply_anchor=result.reply_anchor,
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

    async def _reaction_deliverable(self, event: Dict[str, Any]) -> bool:
        """Only a typed fact on a Host that holds it as context; a row queued before the typed
        fact existed has no such promise, on any Host."""
        discover = getattr(self.submitter, "discover_delivery_support", None)
        if discover:
            await discover()
        return ("reaction" in (event.get("message") or {})
                and getattr(self.submitter, "presence_reaction_context", 0) == 1)

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
                    reply_anchor=result.reply_anchor,
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
            if is_reaction_report(payload):
                # Whatever froze this report, it reaches only a Host that will not count it as speech.
                discover = getattr(self.submitter, "discover_delivery_support", None)
                if discover:
                    await discover()
                capable = getattr(self.submitter, "presence_explicit_completion", 0) == 1
                payload = typed_reaction_report(payload) if capable else None
                if payload is None:
                    self.store.finish_delivery_report(delivery_id, index, error=(
                        REACTION_REPORT_UNREADABLE if capable else REACTION_HISTORY_UNAVAILABLE))
                    return True
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
        if _is_reaction(lease.payload):
            await self._process_reaction(lease)
            return True
        try:
            client = await self._ready_client()
            if client is None:
                raise RuntimeError("telegram token is unavailable")
            lease = replace(lease, payload={**lease.payload, "_reporting": await self._resolved_reporting(lease.payload)})
            self.store.checkpoint_outbox(lease.delivery_id, lease.payload)
            async def before_send():
                await self._check_place(lease.payload)

            receipt = await _deliver(client, lease, self.store, before_send=before_send, account_id=self._bot_id)
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
                # response was lost. Do not execute a second physical edit
                # merely because this lease can be reclaimed.
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

    async def _resolved_reporting(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        reporting = dict(payload.get("_reporting") or {"version": 0})
        discover = getattr(self.submitter, "discover_delivery_support", None)
        if reporting.get("version") == -1:
            reporting["version"] = await discover() if discover else 0
            reporting["status"] = getattr(self.submitter, "delivery_reporting_status", "unsupported")
        if reporting.get("version") == 1 and not reporting.get("account_id"):
            reporting["account_id"] = self._bot_id
        if reporting.get("version") == 1:
            if _is_reaction(payload) and discover:
                await discover()
            reporting["explicit_completion"] = getattr(self.submitter, "presence_explicit_completion", 0)
        return reporting

    async def _check_place(self, payload: Dict[str, Any], *, thread_id: str = "") -> None:
        check = getattr(self.submitter, "place_hold", None)
        if check:
            reason = await check(self._bot_id, str(payload.get("chat_id") or ""),
                                 **({"thread_id": thread_id} if thread_id else {}))
            if reason:
                raise PlaceHeld(reason)

    async def _process_reaction(self, lease: OutboxLease) -> None:
        """One setMessageReaction row, sent at most once.

        ``known`` is the newest thing this process has learned about the row. Telegram can only be
        reached after the marker is durable, so a row without one was not sent by this version.
        A known yes finishes with no token, Host or call; a marker without an answer ends uncertain.
        """
        known = dict(self._unrecorded_reactions.pop(lease.delivery_id, None) or lease.payload)
        if "_operation_result" in known:
            self._finish_reaction(lease, known)
            return
        if lease.previous_unanswered:
            # No marker does not prove no call: 0.9.0 wrote none.
            known[_REACTION_MARKER] = True
        if known.get(_REACTION_MARKER):
            await self._settle_unanswered_reaction(lease, known)
            return
        try:
            client = await self._ready_client()
            if client is None:
                raise RuntimeError("telegram token is unavailable")
            known["_reporting"] = await self._resolved_reporting(known)
            self.store.checkpoint_outbox(lease.delivery_id, known)
            parameters = known.get("parameters")
            if not str(known.get("chat_id") or "").strip() or not isinstance(parameters, dict):
                raise ValueError("Telegram reaction chat_id and parameters are required")
            # The topic the tool's guard checked, so a topic paused after queueing holds this row too.
            await self._check_place(known, thread_id=str(known.get("topic_id") or ""))
            self.store.checkpoint_outbox(lease.delivery_id, {**known, _REACTION_MARKER: True})
            known[_REACTION_MARKER] = True
            result = await client.operation(REACTION_METHOD, dict(parameters))
            if result.get("ok") is False:
                raise TelegramApiError(REACTION_METHOD, "Telegram did not confirm the reaction")
            known["_operation_result"] = result
        except PlaceHeld as exc:
            self.store.hold_outbox(lease.delivery_id, str(exc))
        except asyncio.CancelledError:
            self.store.release_outbox(lease.delivery_id, reason="worker_cancelled", retry_after_sec=0)
            raise
        except Exception as exc:
            self._last_error = _error(exc)
            try:
                self._after_reaction_error(lease, known, exc)
            except Exception as store_error:
                # The row stays leased, and an expired lease is never sent again.
                self._last_error = _error(store_error)
        else:
            self._finish_reaction(lease, known)

    def _after_reaction_error(self, lease: OutboxLease, known: Dict[str, Any], exc: Exception) -> None:
        code = exc.error_code if isinstance(exc, TelegramApiError) else None
        if known.get(_REACTION_MARKER):
            if code in _REACTION_REFUSALS:
                # Telegram answered and refused: no retry can change it, so the lane moves on.
                self._settle_reaction(lease, known, state="failed", error=_error(exc))
                return
            if code not in _REACTION_NOT_DONE:
                self._settle_reaction(lease, known, state="uncertain", error=_error(exc))
                return
            del known[_REACTION_MARKER]
            self.store.checkpoint_outbox(lease.delivery_id, known)
        if lease.attempts >= _MAX_OUTBOX_ATTEMPTS:
            self._settle_reaction(lease, known, state="failed", error=_error(exc))
        else:
            self.store.release_outbox(lease.delivery_id, reason=_error(exc), retry_after_sec=_retry_delay(lease.attempts))

    async def _settle_unanswered_reaction(self, lease: OutboxLease, known: Dict[str, Any]) -> None:
        try:
            if await self._ready_client() is not None:
                known["_reporting"] = await self._resolved_reporting(known)
        except Exception as exc:
            # Neither changes what is known. Without them this outcome stays in the local receipt.
            self._last_error = _error(exc)
        error = _error(TelegramApiError(REACTION_METHOD, "send_result_unrecorded"))
        try:
            self._settle_reaction(lease, known, state="uncertain", error=error)
        except Exception as exc:
            self._last_error = _error(exc)

    def _settle_reaction(self, lease: OutboxLease, known: Dict[str, Any], *, state: str, error: str) -> None:
        self.store.checkpoint_outbox(lease.delivery_id, known)
        report = delivery_report(lease.delivery_id, known, part_id="status", state=state, error=error)
        self.store.mark_outbox_failed(lease.delivery_id, reason=error, report=report, state=state)
        self._last_error = error

    def _finish_reaction(self, lease: OutboxLease, known: Dict[str, Any]) -> None:
        try:
            report = delivery_report(lease.delivery_id, known, part_id="0", state="delivered",
                                     receipt=known["_operation_result"], text=str(known.get("text") or ""), fmt="plain")
            self.store.checkpoint_outbox(lease.delivery_id, known, report=report)
            self.store.mark_delivered(lease.delivery_id, provider_receipt=reaction_receipt(known))
        except Exception as exc:
            self._keep_known_reaction(lease, known, exc)

    def _keep_known_reaction(self, lease: OutboxLease, known: Dict[str, Any], exc: Exception) -> None:
        """Telegram said yes and only the record failed: no later attempt may ask it again.

        If the store cannot take even this, the row stays leased with its marker. This process still
        knows the result and finishes it after the lease expires; a restart first ends it uncertain.
        """
        self._last_error = _error(exc)
        self._unrecorded_reactions[lease.delivery_id] = known
        try:
            self.store.checkpoint_outbox(lease.delivery_id, known)
            self.store.release_outbox(lease.delivery_id, reason=_error(exc), retry_after_sec=_retry_delay(lease.attempts))
        except Exception as store_error:
            self._last_error = _error(store_error)
        else:
            del self._unrecorded_reactions[lease.delivery_id]

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


def _is_reaction(payload: Dict[str, Any]) -> bool:
    return payload.get("kind") == "operation" and payload.get("method") == REACTION_METHOD


def _own_message(account_id: str, chat_id: str, topic_id: Any, result: Dict[str, Any], words: str) -> Optional[Tuple]:
    """A confirmed send's retained row; without its exact provider id there is nothing to remember."""
    message_id = result.get("message_id")
    if not account_id or type(message_id) is not int:
        return None
    chat = result.get("chat") if isinstance(result.get("chat"), dict) else {}
    return (account_id, chat_id, message_id, str(topic_id or ""),
            _chat_forum(chat.get("type"), chat.get("is_forum"), topic_id), account_id, "", 1, str(result.get("text") or result.get("caption") or words),
            str(result.get("media_group_id") or ""))


async def _deliver(
    client: TelegramClient, lease: OutboxLease, store: CustodyStore, *, before_send=None, account_id: str = "",
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
                lease.delivery_id, {**payload, "_sent_messages": messages}, report=report,
                sent=_own_message(account_id, chat_id, topic_id, result, chunk["text"]),
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
    store.checkpoint_outbox(lease.delivery_id, {**payload, "_sent_media": result}, report=report,
                            sent=_own_message(account_id, chat_id, topic_id, result, caption["text"]))
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
