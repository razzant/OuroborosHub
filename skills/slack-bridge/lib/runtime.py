from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import pathlib
import re

import httpx
from typing import Any, Awaitable, Sequence

from .events import REACTION_EVENTS
from .host_adapter import HostBindingTerminalError, HostOutput, HostRetryError, HostTurnStatus, PresenceHostAdapter
from .provider_context import capture_context
from .slack_api import (
    SlackApiError, SlackClient, SlackConfigurationError, SlackMutationUncertain, UploadInput, chunk_message,
    mutation_may_have_applied,
)
from .socket_mode import SocketModeClient
from .store import REACTION_WITHHELD, BridgeStore, InboxItem, InboxLeaseLost, OutboxItem, OutboxLeaseLost

log = logging.getLogger(__name__)
_MAX_OUTBOX_ATTEMPTS = 5
# Bound retransmitted traffic: recover at most twice, using a fresh slot each time.
_MAX_UPLOAD_ATTEMPTS = 3
# Failures before any request left this process: no provider effect is possible.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Slack answers that may change on retry, by their documented descriptions.
_UPLOAD_TEMPORARY_ERRORS = frozenset({"internal_error", "fatal_error", "request_timeout", "service_unavailable"})
_UPLOAD_MALFORMED_ERRORS = frozenset({"invalid_json", "invalid_response", "unknown_error", "missing_upload_url"})
# Budget the Host's 1800-second initial request plus a recovery buffer. A worker
# without a role refreshes once before its finite continuation poll; a companion
# input claim ends at the durable reference and an observation claim gets its own
# budget. Staging or suspension can still outlast this budget; the token checks
# remain the ownership authority.
_INBOUND_LEASE_SECONDS = 2100.0
# Which inbox rows each worker role claims: new input (no Host reference yet),
# admitted work to observe (a stored reference), or both in one claim.
_ROLE_CLAIMS = {None: None, "intake": False, "observe": True}
# A busy conversation or full capacity refusal resubmits the same event after the
# larger of the Host's retry_after and the bridge's own backoff, which starts at one
# second, doubles per refusal and is capped here. The Host's hint is never cut.
_CAPACITY_BACKOFF_MAX_SECONDS = 30.0
# Documented reactions.add / reactions.remove refusals no retry can change, so the lane moves on.
# Rate limits, ambiguous server answers, migrations in progress and anything unlisted keep their
# own handling: https://docs.slack.dev/reference/methods/reactions.add/#errors
_REACTION_REFUSALS = frozenset({
    "access_denied", "account_inactive", "bad_timestamp", "channel_not_found", "deprecated_endpoint",
    "ekm_access_denied", "enterprise_is_restricted", "invalid_arg_name", "invalid_arguments",
    "invalid_array_arg", "invalid_auth", "invalid_name", "is_archived", "message_not_found",
    "method_deprecated", "missing_scope", "no_access", "no_item_specified", "no_permission",
    "not_allowed_token_type", "not_authed", "not_reactable", "team_access_not_granted", "thread_locked",
    "token_expired", "token_revoked", "too_many_emoji", "too_many_reactions", "two_factor_setup_required",
})
# A Host without explicit completion could count the report as speech; the provider receipt stays local.
_REACTION_HISTORY_UNAVAILABLE = "Held: Ouroboros records this reaction after an update."
# Provider observations, not new effects: the requested state already held.
_REACTION_NO_OPS = {("reaction_add", "already_reacted"): "already_present",
                    ("reaction_remove", "no_reaction"): "already_absent"}


def _retry_delay(exc: BaseException, attempts: int) -> float:
    if isinstance(exc, HostRetryError) and exc.code == "presence_capacity_full":
        backoff = min(_CAPACITY_BACKOFF_MAX_SECONDS, 2.0 ** min(max(attempts - 1, 0), 5))
        return backoff if exc.retry_after is None else max(exc.retry_after, backoff)
    return min(60.0, 2.0 ** min(attempts, 5))


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
        presence_identity=getattr(host, "presence_identity", 0),
        connector_identity=getattr(host, "connector_identity", {}),
        history_reporting_state=getattr(host, "delivery_reporting_status", "unsupported"),
        history_reporting_limitation="" if mode else "Host delivery reporting unavailable; provider sending remains enabled.",
        reaction_context_state="available" if _reactions_live(host) else "update_required",
        reaction_context_limitation="" if _reactions_live(host) else (
            "Host context for reactions needs an update: incoming reactions stay in this bridge's "
            "inbox, are not delivered, and are not replayed after the update."),
        reaction_history_limitation="" if getattr(host, "explicit_completion", 0) == 1 else (
            "Host history for reactions needs an update: own reaction writes keep their provider receipt only."),
    )
    return mode


def _is_reaction_report(payload: Any) -> bool:
    message = payload.get("message") if isinstance(payload, dict) else None
    return isinstance(message, dict) and message.get("kind") == "operation" and "reaction" in message


def _reactions_live(host: Any) -> bool:
    return getattr(host, "reaction_context", 0) == 1


def _automatic_origin(item: InboxItem, turn_ref: str) -> dict[str, str]:
    origin = {"kind": "automatic", "source_event_id": item.provider_event_key}
    if turn_ref:
        origin["task_id"] = turn_ref
    return origin


def _output_request_id(item: InboxItem, output: HostOutput) -> str:
    """One outbox identity per Host output and destination, never per text or turn index."""
    key = json.dumps([output.identity, item.team_id, item.channel_id, item.conversation_thread],
                     ensure_ascii=False, separators=(",", ":"))
    return f"presence-output:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _delivery_report(item: Any, state: str, *, result: dict[str, Any] | None = None,
                     error: str = "", conversation_kind: str = "") -> dict[str, Any] | None:
    return item.delivery_report(state, result=result, error=error, conversation_kind=conversation_kind)


class InboundWorker:
    """Submit Slack events to Host and observe the work they admit.

    ``role=None`` does both in one claim. The companion runs ``"intake"``
    workers, which only submit rows without a Host reference and release each
    one as soon as its reference is durable, and ``"observe"`` workers, which only
    poll stored references. A poll therefore never occupies an input worker.
    """

    def __init__(
        self,
        store: BridgeStore,
        slack: SlackClient,
        host: PresenceHostAdapter,
        *,
        staged_root: pathlib.Path,
        role: str | None = None,
    ) -> None:
        if role not in _ROLE_CLAIMS:
            raise ValueError(f"Unknown inbound worker role: {role}")
        self.store = store
        self.slack = slack
        self.host = host
        self.staged_root = staged_root
        self.role = role

    def _enqueue_outputs(self, item: InboxItem, outputs: Sequence[HostOutput], mode: int) -> None:
        for output in outputs:
            chunks = chunk_message(output.text)
            if not chunks:
                continue
            self.store.enqueue_outbox(
                request_id=_output_request_id(item, output),
                target=item.channel_id,
                thread_ts=item.conversation_thread,
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
                    target=item.channel_id, thread_ts=item.conversation_thread, chunks=chunks,
                    origin=_automatic_origin(item, status.turn_ref),
                    delivery_reporting_version=status.delivery_reporting_version,
                )
        self._enqueue_outputs(item, status.outputs, status.delivery_reporting_version)

    async def process_once(self) -> bool:
        if self.role is None:
            item = self.store.claim_inbox(lease_seconds=_INBOUND_LEASE_SECONDS)
        else:
            item = self.store.claim_inbox(lease_seconds=_INBOUND_LEASE_SECONDS,
                                          submitted=_ROLE_CLAIMS[self.role])
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
            reaction = item.event_type in REACTION_EVENTS
            if reaction and not item.host_reference:
                await _discover_reporting(self.host, self.store)
                if not _reactions_live(self.host):
                    # Before any provider read: this Host cannot promise not to wake on it.
                    self.store.withhold_inbox(item.row_id, item.lease_token, REACTION_WITHHELD)
                    return True
            if not item.host_reference and item.provider_context is None:
                retained = (self.store.retained_message(item.team_id, item.channel_id, item.message_ts)
                            if reaction else None)
                snapshot = await capture_context(self.slack, item, self.store.workspace_name(), retained,
                                                 names=self.store)
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
                    queue = self.store.conversation_queue(item, reactions=_reactions_live(self.host))
                    self.store.set_transport_queue(item.row_id, item.lease_token, queue)
                    item = dataclasses.replace(item, transport_queue=queue)
                reference = str(await self.host.submit(item)).strip()
                if not reference:
                    raise RuntimeError(
                        "Presence Host adapter returned an empty reference"
                    )
                self.store.set_host_reference(item.row_id, item.lease_token, reference)
                if self.role == "intake":
                    return await self._settle_submission(item, reference)
                if reference.startswith(("continuing:", "refused:", "retained:")):
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
                        snapshot = self.store.conversation_queue(item, reactions=_reactions_live(self.host))
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

            await self._deliver(item, reference)
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
            # A capacity refusal keeps the same event first in its conversation and frees
            # this worker for other conversations; it is paced by the Host's hint.
            delay = _retry_delay(exc, item.attempts)
            self.store.retry_inbox(
                item.row_id,
                item.lease_token,
                str(exc),
                delay_seconds=delay,
            )
            log.warning("Slack inbound event %s will retry in %.0fs: %s", item.row_id, delay, exc)
            return True

    async def _settle_submission(self, item: InboxItem, reference: str) -> bool:
        """Commit what a just-stored reference already says, then free this input worker.

        Speech its envelope carries is queued first. A reference that still names
        Host work returns to the inbox due now, for an observation worker: the
        conversation's next event no longer waits for it, and no poll or queue
        report runs here.
        """
        initial = getattr(self.host, "initial_status", None)
        status = initial(reference) if initial is not None else HostTurnStatus("pending")
        self._enqueue_status(item, reference, status)
        if status.state == "failed":
            self.store.fail_inbox(item.row_id, item.lease_token, status.error or "Presence Host turn failed")
        elif status.state == "ready":
            await self._deliver(item, reference)
        else:
            self.store.retry_inbox(item.row_id, item.lease_token, status.error, delay_seconds=0.0)
        return True

    async def _deliver(self, item: InboxItem, reference: str) -> None:
        delivery_key = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        delivery = await self.host.deliver(reference)
        self._enqueue_outputs(item, getattr(delivery, "outputs", ()), delivery.delivery_reporting_version)
        for index, text in enumerate(delivery.texts):
            chunks = chunk_message(text)
            if not chunks:
                continue
            self.store.enqueue_outbox(
                request_id=f"presence:{delivery_key}:final:{index}",
                target=item.channel_id,
                thread_ts=item.conversation_thread,
                chunks=chunks,
                origin=_automatic_origin(item, delivery.turn_ref),
                delivery_reporting_version=delivery.delivery_reporting_version,
            )
        self.store.complete_inbox(item.row_id, item.lease_token)


@dataclasses.dataclass
class _UploadAttempt:
    """What one claim of an upload row observed, kept as receipt facts, never as permission.

    ``bytes`` is not_sent, sending, accepted, rejected (an HTTP answer other than 200),
    length_mismatch or unknown; ``completion`` is not_sent, started, completed, refused,
    not_applied (429, or a request that never left) or unknown.
    """

    number: int
    index: int | None = None
    phase: str = "input"
    declared_length: int = 0
    file_id: str = ""
    bytes: str = "not_sent"
    bytes_sent: int = 0
    acknowledged_length: int | None = None
    completion: str = "not_sent"
    outcome: str = ""
    failure: dict[str, Any] | None = None
    # Set once the completion marker is durable: from then on nothing proves the file unshared.
    completion_started: bool = False

    def write(self) -> tuple[int | None, dict[str, Any]]:
        facts: dict[str, Any] = {
            "attempt": self.number, "phase": self.phase, "declared_length": self.declared_length,
            "file_id": self.file_id, "bytes": self.bytes, "bytes_sent": self.bytes_sent,
            "completion": self.completion,
        }
        if self.acknowledged_length is not None:
            facts["acknowledged_length"] = self.acknowledged_length
        if self.outcome:
            facts["outcome"] = self.outcome
        if self.failure:
            facts["failure"] = dict(self.failure)
        return self.index, facts

    def fail(self, exc: BaseException, failure_class: str) -> str:
        """Record this phase's failure without provider bodies or URLs; return its error code."""
        error = (exc.error if isinstance(exc, SlackApiError) else
                 str(exc) if isinstance(exc, SlackConfigurationError) else type(exc).__name__)
        self.failure = {"phase": self.phase, "class": failure_class, "error": error}
        if isinstance(exc, SlackApiError) and exc.status_code:
            self.failure["http_status"] = exc.status_code
        return error


def _upload_failure(exc: BaseException, *, completion_started: bool) -> tuple[str, bool]:
    """``(failure class, retry)`` for one upload attempt; a retry always takes a fresh slot.

    Before the completion marker nothing has been shared: a transient answer or an
    unknown transport outcome may retry, an explicit refusal is final. After it only
    an answer proving the completion was not applied (429, or a request that never
    left) may retry; anything else unknown stays uncertain.
    """
    if isinstance(exc, SlackConfigurationError):
        return "local_input", False
    if isinstance(exc, _NOT_SENT):
        return "not_sent", True
    if isinstance(exc, SlackApiError) and (exc.status_code == 429 or exc.error == "ratelimited"):
        return "rate_limited", True
    if completion_started:
        if isinstance(exc, SlackApiError) and not mutation_may_have_applied(exc):
            return "refused", False
        return "unknown", False
    if isinstance(exc, SlackApiError):
        if exc.error == "upload_bytes_length_mismatch":
            return "length_mismatch", True
        if exc.status_code == 408 or exc.status_code >= 500 or exc.error in _UPLOAD_TEMPORARY_ERRORS:
            return "provider_unavailable", True
        if exc.error in _UPLOAD_MALFORMED_ERRORS:
            return "malformed_response", True
        return "refused", False
    if isinstance(exc, (httpx.HTTPError, TimeoutError, OSError)):
        return "transport_unknown", True
    return "internal", True


def _bytes_outcome(exc: BaseException, sent: int) -> str:
    if isinstance(exc, SlackApiError) and exc.error == "upload_bytes_length_mismatch":
        return "length_mismatch"
    if isinstance(exc, SlackApiError) and exc.status_code:
        return "rejected"
    if not sent and isinstance(exc, (*_NOT_SENT, SlackConfigurationError)):
        return "not_sent"
    return "unknown"


def _remove_staged(artifact: pathlib.Path) -> None:
    """Remove a staged upload copy after its row's terminal checkpoint; never the caller's original."""
    if not str(artifact) or artifact == pathlib.Path():
        return  # A row without a staged path names no file.
    try:
        artifact.unlink(missing_ok=True)
    except OSError:
        log.warning("Slack staged mutation artifact cleanup failed: %s", artifact)


class OutboundWorker:
    # One claim's lease, and how often a running upload extends that same lease.
    lease_seconds = 120.0
    lease_renew_seconds = 30.0

    def __init__(self, store: BridgeStore, slack: SlackClient, host: Any = None) -> None:
        self.store = store
        self.slack = slack
        self.host = host
        self._report_task: asyncio.Task[None] | None = None

    async def _place_hold(self, item: OutboxItem, target: str) -> str:
        check = getattr(self.host, "place_hold", None)
        if check is None:  # Legacy adapters do not advertise places.
            return ""
        # A member ID still needs conversations.open before it names a room.
        conversation = "" if target == "mutation" or target.startswith(("U", "W")) else target
        # A reaction is checked in the thread its receipt names; an unretained target stays the room's.
        retained = self._reaction_target(item) if conversation else None
        thread = str(retained["thread_ts"]) if retained else ""
        return await check(item.provider_account_id, conversation, **({"thread_id": thread} if thread else {}))

    async def _held(self, item: OutboxItem, target: str) -> bool:
        reason = await self._place_hold(item, target)
        if reason:
            self.store.hold_outbox(item, reason)
            return True
        return False

    async def _report(self, report: dict[str, Any]) -> None:
        try:
            if _is_reaction_report(report["payload"]):
                # A report frozen earlier reaches only a Host that will not count it as speech.
                discover = getattr(self.host, "discover_delivery_support", None)
                if discover is not None:
                    await discover()
                if getattr(self.host, "explicit_completion", 0) != 1:
                    self.store.finish_report(report, error=_REACTION_HISTORY_UNAVAILABLE)
                    return
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

    def _reaction_report(self, item: OutboxItem, state: str, *, effect: str = "",
                         error: str = "") -> dict[str, Any] | None:
        """A Host that keeps a final answer past an action hears of it; others keep the local receipt."""
        if item.operation not in {"reaction_add", "reaction_remove"} or getattr(self.host, "explicit_completion", 0) != 1:
            return None
        return item.reaction_report(state, retained=self._reaction_target(item), effect=effect, error=error)

    def _reaction_target(self, item: OutboxItem) -> dict[str, Any] | None:
        """A reaction row's retained target, the only source of its thread; ``None`` for any other row."""
        if item.operation not in {"reaction_add", "reaction_remove"}:
            return None
        return self.store.retained_message(item.provider_account_id, str(item.payload.get("channel") or ""),
                                           str(item.payload.get("ts") or ""))

    def _cancel_started_send(self, item: OutboxItem, *,
                             upload_attempt: tuple[int | None, dict[str, Any]] | None = None) -> bool:
        try:
            self.store.fail_outbox(
                item.row_id, item.lease_token, "send_cancelled_after_dispatch", state="uncertain",
                result={"uncertain": True},
                report_payload=_delivery_report(item, "uncertain", error="send_cancelled_after_dispatch")
                if item.kind == "text" else
                self._reaction_report(item, "uncertain", error="send_cancelled_after_dispatch"),
                upload_attempt=upload_attempt,
            )
        except Exception:
            # The durable attempt marker still prevents a resend if checkpointing
            # fails, or another worker has already recovered this expired lease.
            log.exception("Slack cancelled send %s could not checkpoint uncertainty", item.row_id)
            return False
        return True

    async def process_once(self) -> bool:
        # A slow callback keeps its own task while this worker continues sending.
        reported = self._advance_reporting()
        item = self.store.claim_outbox(lease_seconds=self.lease_seconds)
        if item is None:
            return reported
        try:
            return await self._process_claimed(item, reported)
        except OutboxLeaseLost:
            # Lease recovery or a newer claim owns this row now. Only that owner writes it
            # or removes its staged file; this obsolete attempt ends without another write.
            log.info("Slack outbox item %s lease lost; ending obsolete attempt", item.row_id)
            return True

    async def _process_claimed(self, item: OutboxItem, reported: bool) -> bool:
        item = dataclasses.replace(item, provider_account_id=(
            item.provider_account_id or str(self.store.runtime_value("workspace_id", ""))
        ))
        if await self._held(item, item.resolved_channel or item.target):
            return True
        if item.kind == "mutation":
            return await self._process_mutation(item) or reported
        send_started = False
        try:
            channel = item.resolved_channel or await self.slack.resolve_target(item.target)
            self.store.set_resolved_target(item, channel, item.provider_account_id)
            item = dataclasses.replace(item, resolved_channel=channel)
            if await self._held(item, channel):
                return True
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
                report_payload=_delivery_report(
                    item, "delivered", result=result,
                    conversation_kind=self.store.conversation_kind(item.provider_account_id, actual_channel)),
            )
            return True

        except asyncio.CancelledError:
            if send_started:
                self._cancel_started_send(item)
            raise
        except OutboxLeaseLost:
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
            before_request = isinstance(exc, _NOT_SENT)
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

    async def _keep_lease(self, item: OutboxItem, attempt: _UploadAttempt, work: Awaitable[bool]) -> bool:
        """Run one upload attempt as a child task, extending this claim's lease while it runs.

        The same lease is extended every ``lease_renew_seconds`` until the child ends,
        and the child is joined on every exit: nothing outlives the attempt and the
        lease costs no provider request. A refused extension (claim expired or
        replaced) cancels the attempt before its completion marker, when nothing has
        been shared. After the marker a live completion is never cut short; its own
        checkpoint then finds the claim gone.
        """
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=self.lease_renew_seconds)
                if done:
                    return task.result()
                try:
                    self.store.renew_outbox_lease(item, lease_seconds=self.lease_seconds)
                except OutboxLeaseLost:
                    if attempt.completion_started:
                        return await task
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                except Exception:
                    # A failed write is not lease loss; the attempt's next checkpoint decides.
                    log.warning("Slack upload %s lease extension failed", item.request_id, exc_info=True)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _process_upload(self, item: OutboxItem) -> bool:
        """One physical upload of a staged file: a fresh slot, streamed bytes, then completion.

        Each phase's facts are durable before the next provider phase, and writing them
        checks this claim. Only completion shares the file, so the send marker is set
        just before it, after a last Place check. Until the marker a failure that may
        change on retry takes a fresh slot, at most three uploads in all; after it an
        unknown outcome stays uncertain and is never repeated.
        """
        artifact = pathlib.Path(str(item.payload.get("path") or ""))
        if item.attempts > _MAX_UPLOAD_ATTEMPTS:
            # Earlier claims spent every upload, including any that stopped before a request.
            self.store.fail_outbox(item.row_id, item.lease_token, "upload_attempts_exhausted",
                                   result={"uncertain": False})
            _remove_staged(artifact)
            return True
        attempt = _UploadAttempt(number=item.attempts)
        return await self._keep_lease(item, attempt, self._upload_attempt(item, artifact, attempt))

    async def _upload_attempt(self, item: OutboxItem, artifact: pathlib.Path, attempt: _UploadAttempt) -> bool:
        payload = item.payload
        filename = str(payload.get("filename") or "upload")
        try:
            source = UploadInput(artifact)
        except SlackConfigurationError as exc:
            # Absent, empty or unreadable before any provider request: a definite local refusal.
            error = attempt.fail(exc, "local_input")
            attempt.outcome = "failed"
            self.store.fail_outbox(item.row_id, item.lease_token, error, result={"uncertain": False},
                                   upload_attempt=attempt.write())
            _remove_staged(artifact)
            return True
        with source:
            try:
                attempt.declared_length, attempt.phase = source.length, "slot"
                attempt.index = self.store.record_upload_attempt(item, *attempt.write())
                file_id, upload_url = await self.slack.allocate_upload(filename=filename, length=source.length)
                attempt.file_id, attempt.phase = file_id, "bytes"
                # The slot's identity is durable before any byte leaves.
                attempt.index = self.store.record_upload_attempt(item, *attempt.write())
                if await self._upload_held(item, attempt):
                    return True
                # Durable before the POST: from here a crash or lost claim must not read as unsent.
                attempt.bytes = "sending"
                try:
                    attempt.index = self.store.record_upload_attempt(item, *attempt.write())
                except BaseException:
                    attempt.bytes = "not_sent"  # The POST never started.
                    raise
                try:
                    attempt.acknowledged_length = await self.slack.send_upload_bytes(upload_url, source)
                finally:
                    attempt.bytes_sent = source.sent
                attempt.bytes, attempt.phase = "accepted", "completion"
                attempt.index = self.store.record_upload_attempt(item, *attempt.write())
                if await self._upload_held(item, attempt):
                    return True
                attempt.completion = "started"
                try:
                    self.store.begin_send(item, upload_attempt=attempt.write())
                except BaseException:
                    attempt.completion = "not_sent"  # No marker, so no completion request.
                    raise
                attempt.completion_started = True
                result = await self.slack.complete_upload(
                    file_id=file_id, title=str(payload.get("title") or filename),
                    channel=str(payload.get("channel") or ""), thread_ts=str(payload.get("thread_ts") or ""),
                    initial_comment=str(payload.get("initial_comment") or ""),
                )
            except asyncio.CancelledError:
                self._upload_cancelled(item, artifact, attempt)
                raise
            except OutboxLeaseLost:
                raise
            except Exception as exc:
                return self._upload_failed(item, artifact, attempt, exc)
        attempt.completion, attempt.outcome = "completed", "delivered"
        try:
            self.store.complete_outbox(item.row_id, item.lease_token, result=result, upload_attempt=attempt.write())
        except OutboxLeaseLost:
            log.warning("Slack upload %s attempt %s shared file %s after its claim was lost; "
                        "the row's current owner keeps its receipt", item.request_id, attempt.number, file_id)
            raise
        except Exception:
            # The share happened but is not recorded. The durable marker stays: lease
            # recovery settles the row uncertain, and nothing is shared again.
            log.exception("Slack upload %s shared file %s but its receipt could not be recorded",
                          item.request_id, file_id)
            return True
        _remove_staged(artifact)
        return True

    async def _upload_held(self, item: OutboxItem, attempt: _UploadAttempt) -> bool:
        """A Place hold after the slot was allocated: custody stays, this attempt's upload is spent."""
        reason = await self._place_hold(item, item.target)
        if not reason:
            return False
        attempt.outcome = "held"
        attempt.failure = {"phase": attempt.phase, "class": "held", "error": reason}
        self.store.hold_outbox(item, reason, spend_attempt=True, upload_attempt=attempt.write())
        return True

    def _upload_cancelled(self, item: OutboxItem, artifact: pathlib.Path, attempt: _UploadAttempt) -> None:
        if attempt.completion_started:
            # Cancellation never proves the completion did not share the file.
            attempt.completion, attempt.outcome = "unknown", "uncertain"
            attempt.failure = {"phase": "completion", "class": "cancelled", "error": "send_cancelled_after_dispatch"}
            if self._cancel_started_send(item, upload_attempt=attempt.write()):
                _remove_staged(artifact)
            return
        # Nothing was shared: the lease lapses and a later claim uploads again with a fresh slot.
        if attempt.bytes == "sending":
            attempt.bytes = "unknown" if attempt.bytes_sent else "not_sent"
        attempt.outcome = "cancelled"
        attempt.failure = {"phase": attempt.phase, "class": "cancelled", "error": "CancelledError"}
        try:
            self.store.record_upload_attempt(item, *attempt.write())
        except Exception:
            log.info("Slack upload %s cancelled attempt %s kept its earlier facts", item.request_id, attempt.number)

    def _upload_failed(self, item: OutboxItem, artifact: pathlib.Path, attempt: _UploadAttempt,
                       exc: BaseException) -> bool:
        failure_class, retry = _upload_failure(exc, completion_started=attempt.completion_started)
        error = attempt.fail(exc, failure_class)
        # A malformed allocation can still have returned an identity. Keep that
        # observation even when the upload URL was absent and no byte POST began.
        if attempt.phase == "slot" and isinstance(exc, SlackApiError):
            attempt.file_id = str(exc.details.get("file_id") or attempt.file_id)
        if isinstance(exc, SlackApiError) and isinstance(exc.details.get("acknowledged_length"), int):
            attempt.acknowledged_length = exc.details["acknowledged_length"]
        if attempt.completion_started:
            attempt.completion = "not_applied" if retry else "refused" if failure_class == "refused" else "unknown"
        elif attempt.bytes == "sending":
            attempt.bytes = _bytes_outcome(exc, attempt.bytes_sent)
        if retry and item.attempts < _MAX_UPLOAD_ATTEMPTS:
            attempt.outcome = "retry"
            delay = (exc.retry_after if isinstance(exc, SlackApiError) and exc.retry_after
                     else min(60.0, 2.0 ** min(item.attempts, 5)))
            self.store.retry_outbox(item.row_id, item.lease_token, error, delay_seconds=delay,
                                    upload_attempt=attempt.write())
            log.warning("Slack upload %s attempt %s will retry with a fresh slot in %.0fs: %s",
                        item.request_id, attempt.number, delay, error)
            return True
        uncertain = attempt.completion_started and failure_class == "unknown"
        state = "uncertain" if uncertain else "failed"
        attempt.outcome = state
        if retry:
            error = f"upload_attempts_exhausted: {error}"
        self.store.fail_outbox(item.row_id, item.lease_token, error, state=state,
                               result={"uncertain": uncertain}, upload_attempt=attempt.write())
        _remove_staged(artifact)
        log.warning("Slack upload %s ended %s at attempt %s: %s", item.request_id, state, attempt.number, error)
        return True

    async def _process_mutation(self, item: OutboxItem) -> bool:
        if item.operation == "upload_file":
            return await self._process_upload(item)
        payload = item.payload
        send_started = False
        try:
            operation = item.operation
            # Effect based, including a generic write made with GET.
            self.store.begin_send(item, uncertain_report=self._reaction_report(
                item, "uncertain", error="send_result_unrecorded"))
            send_started = True
            if operation == "update_message":
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
            if operation in {"reaction_add", "reaction_remove"}:
                report = self._reaction_report(item, "delivered", effect="applied")
                result = {**result, "effect": "applied"}
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
                    report = _delivery_report(
                        item, "delivered", result=result,
                        conversation_kind=self.store.conversation_kind(item.provider_account_id, channel))
                    if report:
                        report["message"].update(operation=payload.get("path"), provider_message_id=timestamp)
            self.store.complete_outbox(item.row_id, item.lease_token,
                                       provider_message_ts=str(result.get("ts") or "") if isinstance(result, dict) else "",
                                       report_payload=report, result=result)
            return True
        except asyncio.CancelledError:
            if send_started:
                self._cancel_started_send(item)
            raise
        except OutboxLeaseLost:
            raise
        except SlackConfigurationError as exc:
            # Mutation arguments were rejected before any provider request (a method
            # slack_api may not call included), despite the conservative marker.
            self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state="failed",
                                   result={"uncertain": False},
                                   report_payload=self._reaction_report(item, "failed", error=str(exc)))
            return True
        except SlackMutationUncertain as exc:
            self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="uncertain", result={"uncertain": True},
                                   report_payload=self._reaction_report(item, "uncertain", error=exc.error))
            log.warning("Slack mutation %s is uncertain after provider acceptance boundary: %s", item.request_id, exc)
            return True
        except SlackApiError as exc:
            no_op = _REACTION_NO_OPS.get((item.operation, exc.error))
            if no_op:
                self.store.complete_outbox(item.row_id, item.lease_token,
                                           report_payload=self._reaction_report(item, "delivered", effect=no_op),
                                           result={"effect": no_op, "provider_error": exc.error, "uncertain": False})
                return True
            if exc.status_code == 429 or exc.error == "ratelimited":
                if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                    self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="failed",
                                           result={"uncertain": False},
                                           report_payload=self._reaction_report(item, "failed", error=exc.error))
                else:
                    self.store.retry_outbox(item.row_id, item.lease_token, exc.error,
                                            delay_seconds=exc.retry_after or min(60.0, 2.0 ** min(item.attempts, 5)))
                return True
            provider_may_have_applied = send_started and mutation_may_have_applied(exc)
            if provider_may_have_applied:
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="uncertain",
                                       result={"uncertain": True},
                                       report_payload=self._reaction_report(item, "uncertain", error=exc.error))
                return True
            if item.operation == "generic_api" or exc.error in {
                "already_reacted", "message_not_found", "cant_update_message", "missing_scope",
                "not_in_channel", "channel_not_found", "invalid_arguments", "invalid_auth",
                "not_allowed_token_type", "file_not_found", "invalid_channel",
                # conversations.join refusals that no retry can change.
                "already_in_channel", "is_archived", "method_not_supported_for_channel_type",
            } or (item.operation in {"reaction_add", "reaction_remove"} and exc.error in _REACTION_REFUSALS):
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="failed",
                                       result={"uncertain": False},
                                       report_payload=self._reaction_report(item, "failed", error=exc.error))
                return True
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state="failed",
                                       result={"uncertain": False},
                                       report_payload=self._reaction_report(item, "failed", error=exc.error))
                return True
            self.store.retry_outbox(item.row_id, item.lease_token, exc.error, delay_seconds=exc.retry_after or min(60.0, 2.0 ** min(item.attempts, 5)))
            return True
        except Exception as exc:
            before_request = isinstance(exc, _NOT_SENT)
            if send_started and not before_request:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state="uncertain",
                                       result={"uncertain": True},
                                       report_payload=self._reaction_report(item, "uncertain", error=type(exc).__name__))
                return True
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state="failed", result={"uncertain": False},
                                       report_payload=self._reaction_report(item, "failed", error=type(exc).__name__))
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
        new_root_channels: Sequence[str] = (),
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
            new_root_channels=new_root_channels,
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
        if self.host.available or getattr(self.host, "delivery_reporting_status", "") == "unavailable":
            staged_root = self.store.state_dir / "staged"
            # Two pools of the configured size: input is submitted by workers that
            # never poll, and stored references are observed by their own workers.
            for role, prefix in (("intake", "slack-inbound"), ("observe", "slack-observe")):
                for index in range(self.inbound_workers):
                    worker = InboundWorker(
                        self.store,
                        self.slack,
                        self.host,
                        staged_root=staged_root,
                        role=role,
                    )
                    self._tasks.append(
                        asyncio.create_task(
                            _worker_loop(worker, self._stop),
                            name=f"{prefix}-{index}",
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
