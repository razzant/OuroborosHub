"""Bounded Telegram provider lifecycle and presence Host integration."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import re
import sqlite3
import threading
import time
from dataclasses import replace
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from . import outbox_files
from .api import (
    MEDIA_METHODS,
    TelegramApiError,
    TelegramClient,
    TelegramFileTooLarge,
    TelegramFileUnavailable,
    TelegramRequestNotSent,
    TelegramTokenMalformed,
    _split_text,
)
from .custody import (
    CALL_STARTED,
    OWNER_BOT_CHANGED,
    REACTION_WITHHELD,
    CustodyStore,
    InboxLease,
    OutboxLease,
    _chat_forum,
    recorded_calls,
    recorded_owner_mutation,
)
from .delivery import (
    REACTION_HISTORY_UNAVAILABLE,
    REACTION_REPORT_UNREADABLE,
    delivery_report,
    is_reaction_report,
    names_message,
    reaction_receipt,
    typed_reaction_report,
)
from .discovery import feed_sighting, group_sightings
from .events import parse_telegram_update, too_large_marks, too_large_note
from .formatting import prepare_caption, prepare_text
from .host import (
    HOLD_PLACE_NOT_CONNECTED,
    HoldNote,
    PLACE_POLICY_UNREAD,
    PresenceHostHTTPError,
    PresenceSubmission,
    PresenceWorkResult,
)
from .owner_feed import (
    HOLD_ACCOUNT_MISMATCH,
    HOLD_AUTH_UNAVAILABLE,
    HOLD_BINDING_CHANGED,
    HOLD_RELAY_BUSY,
    OwnerFeedClient,
    OwnerFeedError,
    RELAY_API_SERVER,
    RelayDownloadTimeout,
    RelayRefusalError,
    SOURCE_OWNER_BOT,
    SOURCE_STANDALONE,
    canonical_bot_id,
    feed_item_place_binding,
    normalize_place_binding,
    token_bot_id_prefix,
)
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
# Attachment outcomes that are settled: never downloaded again, never passed to the Host as files.
_NOT_STAGED = frozenset({"too_large", "failed", "unsupported"})
# A download failure that may pass later keeps its event first in its conversation. It becomes a terminal
# "failed" record only after this many failures that say something about the file, and at least this long after
# the first of them: about 34 minutes at the inbox backoff, never sooner, while later messages of the chat wait.
_DOWNLOAD_FAILURES_BEFORE_FAILED = 6
_DOWNLOAD_FAILING_SECONDS = 1800.0
# A download that got no answer at all counts against its file only if Telegram answered this bot's poll after
# the file's previous failure and this recently: a sleeping Mac or a network still coming up holds nothing against it.
_POLL_EVIDENCE_SECONDS = 120.0
# "typing…": renewed this often (Telegram shows it about 5 s), for at most this long after the model asked.
_TYPING_RENEW_SECONDS = 4.5
_TYPING_CAP_SECONDS = 120.0
# Telegram's answers that it did not perform a send call: the request was wrong or not allowed (400, 403), the token
# names no bot (401, 404), or a flood wait (429). With a request proved never to have left (api.TelegramRequestNotSent),
# only these let the same call be made again; any other outcome of a send call ends its row uncertain.
_NOT_PERFORMED = frozenset({400, 401, 403, 404, 429})
# The relay's 403 refusals, made before any provider call (wire v2). A hold waits for something that can change (a
# Silent place, groups Off, the bridge restarting, a busy relay) and uses no attempt; the rest end the row unsent.
_RELAY_HOLD_REFUSALS = frozenset({
    "place_not_active",
    "not_connected",
    "feed_off",
    "account_mismatch",
    "bridge_not_ready",
    "busy",
    "auth_unavailable",
    "feed_unavailable",
})
# ``binding_changed``: the connection the row was bound to was disconnected or connected again, which never undoes
# itself (a new connection has a new revision), so the row ends instead of waiting for ever.
_RELAY_TERMINAL_REFUSALS = frozenset({
    "method_not_allowed",
    "argument_not_allowed",
    "file_not_offered",
    "file_missing",
    "file_changed",
    "auth_blocked",
    "connector_mismatch",
    "binding_changed",
})
# A download whose connection ended (binding_changed, or its place gone: not_connected on a read) settles its
# attachment as not received, so the event reaches Ouroboros, which answers for that connection, and never loops.
_RELAY_DOWNLOAD_TERMINAL_REFUSALS = frozenset({
    "method_not_allowed",
    "argument_not_allowed",
    "file_not_offered",
    "file_missing",
    "file_changed",
    "binding_changed",
    "not_connected",
})
_MODERATION_METHODS = {
    "delete_message": "deleteMessage",
    "restrict_member": "restrictChatMember",
    "ban_member": "banChatMember",
    "unban_member": "unbanChatMember",
}
# A file request's observation (telegram_receipt's transfer) is stored at most this often while its bytes move, and at
# once when its phase changes or it ends. Each store keeps the row's lease at least _UPLOAD_LEASE_SECONDS from then.
_TRANSFER_RECORD_SECONDS = 1.0
_UPLOAD_LEASE_SECONDS = 300.0
# The condition telegram_send keeps in a conditional row (Ouroboros's whole owner direct-chat condition). It is read
# only for a lease of the queue's conditional class (OutboxLease.conditional), never from a tool argument.
OWNER_DM = "_owner_dm"
# A conditional row without a usable current bot or private-chat check: unsent, held and rechecked.
OWNER_DM_WAITS = "Held: This waits for the same bot and your confirmed direct chat."


class PlaceHeld(RuntimeError):
    """Unsent custody waits for the current place policy, without a provider failure."""


class RetiredTarget(RuntimeError):
    """The retained bot, place connection or owner confirmation cannot be used again. The still-unsent remainder
    ends failed, without rebinding the row or changing what earlier parts confirmed."""


def _hold_kind(exc: PlaceHeld) -> str:
    """The Host answer's type a hold came from (host.HoldNote.kind), "" for an ordinary pause."""
    return str(getattr(exc.args[0], "kind", "") or "") if exc.args else ""


def _relay_hold_kind(refusal: str) -> str:
    if refusal == "account_mismatch":
        return HOLD_ACCOUNT_MISMATCH
    if refusal == "not_connected":
        return HOLD_PLACE_NOT_CONNECTED
    if refusal == "busy":
        return HOLD_RELAY_BUSY
    if refusal == "auth_unavailable":
        return HOLD_AUTH_UNAVAILABLE
    return ""


def _relay_hold_retry_after(exc: RelayRefusalError) -> float:
    if exc.retry_after is not None:
        return max(1.0, float(exc.retry_after))
    if exc.relay_refusal == "busy":
        return 1.0
    return 5.0


def _record_outbox_calls(payload: Dict[str, Any], *, is_owner_bot: bool) -> bool:
    """Whether an outbox row records CALL_STARTED before dispatch and settles from its durable record."""
    return recorded_calls(payload) or (is_owner_bot and recorded_owner_mutation(payload))


class AlbumIncomplete(TelegramApiError):
    """Telegram accepted an album (ok) but its answer does not name one message per item: never sent again."""


class UnconfirmedSend(RuntimeError):
    """Telegram answered ok to a send call but named no message it created (no positive integer message_id), or the
    row's stored answer does not: the call's effect is unknown, so the row ends uncertain and the call is not made
    again."""

    def __init__(self, method: str):
        super().__init__(f"Telegram {method} answered ok without the message it created (no message_id)")


class LeaseLost(RuntimeError):
    """This attempt no longer holds its outbox row: another claim took it (a reload's runtime, or a claim after this
    lease expired). The row is the other attempt's to change. ``message_ids`` names the messages Telegram confirmed to
    this attempt's call that it could not record there, its yes having come after that claim; the outbox logs them."""

    def __init__(self, message: str, message_ids: Sequence[int] = ()):
        super().__init__(message)
        self.message_ids = list(message_ids)


class PresenceSubmitter(Protocol):
    """The frozen provider-to-Host contract used by the runtime."""

    async def submit(
        self, event: Dict[str, Any], staged_files: Sequence[pathlib.Path]
    ) -> PresenceSubmission: ...

    async def poll(self, work_ref: str, binding_id: str) -> PresenceWorkResult: ...


# Why confirmed_bot() offers no bot; the status summary reads the same fixed words (status_summary.py).
BOT_NOT_RUNNING = "Telegram bot not running"
BOT_TOKEN_MISSING = "Bot token missing"
BOT_UNCONFIRMED = "Bot identity is unconfirmed; check the transport status"
BOT_CONFLICT_WITH_OWNER = "Same bot as your personal Telegram chat; polling paused"
# A held row's words while the standalone bot waits to contact Telegram (TelegramTransportRuntime._wait).
_WAIT_FOR_HOST = "Waiting for Ouroboros to answer before this bot contacts Telegram."
_WAIT_FOR_CLAIM = "Waiting for Ouroboros to confirm this bot may be used here."
_WAIT_CONFLICT = "This token is your own Telegram bot's, which Ouroboros reads itself; nothing is sent with it here."
_WAIT_TOKEN_CHANGED = "The bot token changed; the new one is confirmed first."
_WAIT_UNNAMED = "Telegram did not say which bot this token is; it is asked again."


# Where a poll attempt stopped (``poll_stage``): confirming the bot (getMe, and recording the bot it names), asking
# Telegram for new updates, or recording Telegram's answer on this computer.
POLL_STAGES = ("identity", "updates", "record")


def _poll_failure(exc: Exception, stage: str) -> str:
    """Which side a failed poll attempt stopped on: ``token``, ``token_format``, ``network``, ``telegram``, ``local``
    or ``""``.

    ``local``: this computer failed, so neither the token nor the network is the cause: anything after Telegram's
    answer was received (``stage`` "record"), or the custody store (SQLite) at any stage, such as recording the bot
    getMe confirmed. ``token_format``: the configured token cannot be written in a request (a space, a tab or another
    character a token cannot have), so no request left this computer. TelegramClient wraps every failure of its own
    requests in TelegramApiError; any other error leaves the side unknown.
    """
    if stage == "record" or isinstance(exc, sqlite3.Error):
        return "local"
    if isinstance(exc, TelegramTokenMalformed):
        return "token_format"
    if isinstance(exc, OwnerFeedError):
        return "local"
    if isinstance(exc, TelegramApiError):
        if exc.error_code in _TOKEN_REFUSED:
            return "token"
        return "telegram" if exc.error_code else "network"
    return ""


# Which loop recorded the newest error, by the name this runtime gives its asyncio tasks (see run()).
_ERROR_SOURCES = (
    ("telegram-presence-outbox", "outbound"),
    ("telegram-presence-typing", "outbound"),
    ("telegram-presence-inbox-", "inbound"),
    ("telegram-presence-work", "inbound"),
)


def _error_source() -> str:
    """``outbound``, ``inbound`` or ``polling`` (the getUpdates/feed loop runs in run() itself); ``""`` off the loop."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return ""
    if task is None:
        return ""
    name = task.get_name()
    for prefix, source in _ERROR_SOURCES:
        if name.startswith(prefix):
            return source
    return "polling"


class TelegramTransportRuntime:
    # Every assignment of ``self._last_error`` also records when and from which loop, without touching its writers:
    # one immutable (text, time, loop) record, built first and then swapped in whole. The status route reads it from
    # another thread; it reads the record once, so it never pairs one loop's text with another loop's source or time.
    _last_error_record: Tuple[str, float, str] = ("", 0.0, "")

    @property
    def _last_error(self) -> str:
        return self._last_error_record[0]

    @_last_error.setter
    def _last_error(self, value: str) -> None:
        self._last_error_record = (value, time.time(), _error_source()) if value else ("", 0.0, "")

    def __init__(
        self,
        *,
        state_dir: pathlib.Path,
        token_provider: Callable[[], str],
        logger: Any,
        submitter: PresenceSubmitter,
        client_factory: Callable[[str], TelegramClient] = TelegramClient,
        inbox_workers: int = 2,
        source_mode: str = SOURCE_STANDALONE,
        settings_dir: Optional[pathlib.Path] = None,
        owner_feed: Optional[OwnerFeedClient] = None,
        relay_transport_factory: Optional[Callable[[str, pathlib.Path], Any]] = None,
        store_lazy: bool = False,
        capability_source: Optional[Callable[[], Any]] = None,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self._settings_dir = pathlib.Path(settings_dir) if settings_dir is not None else self.state_dir
        self.source_mode = str(source_mode or SOURCE_STANDALONE)
        self._store_path = self.state_dir / "custody.sqlite3"
        self._store: Optional[CustodyStore] = (
            None if store_lazy else CustodyStore.shared(self._store_path, owner_bot=self.is_owner_bot))
        self.token_provider = token_provider
        self.logger = logger
        self.submitter = submitter
        self.client_factory = client_factory
        self.owner_feed = owner_feed
        self.relay_transport_factory = relay_transport_factory
        self._capability_source = capability_source
        self.inbox_workers = max(1, min(8, int(inbox_workers)))
        self._stopping = False
        self._stop_event: Optional[asyncio.Event] = None
        self._client: Optional[TelegramClient] = None
        self._client_token = ""
        self._conflict_token = ""
        self._conflict_bot_id = ""
        # Why _ready_client gave no client while the token is set (arbitration with the owner's own bot): a wait that
        # holds a worker's row without using an attempt, never "token unavailable". "" when it gave one or no token.
        self._ready_wait = ""
        self._client_lock: Optional[asyncio.Lock] = None
        self._run_task: Optional[asyncio.Task[Any]] = None
        self._bot_id = ""
        self._bot_username = ""
        self._bot_label = "not connected"
        self._runtime_state = "not_started"
        self._feed_observed = False
        self._feed_enabled = False
        self._feed_reason = ""
        self._off_account_id = ""
        self._owner_identity: Optional[Dict[str, Any]] = None
        self._invite_url = ""
        self._last_error = ""
        # Telegram's yes that the store could not take yet, by delivery id. See _keep_known_reaction.
        self._unrecorded_reactions: Dict[str, Dict[str, Any]] = {}
        # When getUpdates/feed last answered: evidence that a download failing meanwhile is that file's own trouble.
        self._provider_ok_at = 0.0
        # The last COMPLETED poll attempt and the token/account it ran with: "ok" only once its answer was recorded.
        self._poll_attempt: Dict[str, Any] = {}
        # Guards the link facts the status reads together (bot identity, its token, runtime state, poll attempt):
        # the status route reads them off the event loop thread, so one answer never mixes two bots.
        self._facts_lock = threading.Lock()
        # Per conversation (chat_id, topic_id): the last "typing…" sent, and the last message this process delivered.
        self._typing_sent_at: Dict[Tuple[str, str], float] = {}
        self._delivered_at: Dict[Tuple[str, str], float] = {}

    @property
    def is_owner_bot(self) -> bool:
        return self.source_mode == SOURCE_OWNER_BOT

    @property
    def store_exists(self) -> bool:
        return self._store is not None or self._store_path.is_file()

    @property
    def store(self) -> CustodyStore:
        if self._store is None:
            self._store = CustodyStore.shared(self._store_path, owner_bot=self.is_owner_bot)
        return self._store

    @store.setter
    def store(self, value: CustodyStore) -> None:
        self._store = value

    def _start_workers(self, workers: List[asyncio.Task[Any]]) -> None:
        if workers:
            return
        # Single owner per state dir and no worker of this runtime has started,
        # so any leased row is stale (see CustodyStore.release_own_leases).
        released = self.store.release_own_leases()
        if any(released.values()):
            self._log("info", f"telegram-bot: released stale leases {released}")
        self.store.clear_typing()  # a "typing…" request never outlives the runtime that would show it
        for index in range(self.inbox_workers):
            workers.append(asyncio.create_task(
                self._inbox_worker(), name=f"telegram-presence-inbox-{index}"
            ))
        workers.append(
            asyncio.create_task(self._work_worker(), name="telegram-presence-work")
        )
        workers.append(
            asyncio.create_task(self._outbox_worker(), name="telegram-presence-outbox")
        )
        workers.append(asyncio.create_task(self._typing_worker(), name="telegram-presence-typing"))

    async def run(self) -> None:
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._client_lock = asyncio.Lock()
        self._run_task = asyncio.current_task()
        workers: List[asyncio.Task[Any]] = []
        try:
            if self.is_owner_bot:
                await self._feed_loop(workers)
            else:
                self._start_workers(workers)
                await self._poll_loop()
        finally:
            self._stopping = True
            if self._stop_event is not None:
                self._stop_event.set()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            with self._facts_lock:
                self._runtime_state = "stopped"
                self._run_task = None
            if self.is_owner_bot and isinstance(self.owner_feed, OwnerFeedClient):
                self.owner_feed.connection_identity = None
            # The workers' releases are committed; a later run or status read opens it again.
            if self._store is not None:
                self._store.close()

    def stop(self) -> None:
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        elif self._store is not None:
            # Nothing runs to close it on its way out, as in a pooled worker, whose runtime never starts.
            self._store.close()

    def confirmed_bot(self) -> Tuple[str, str]:
        """The bot id confirmed for this runtime now, else ``""`` and why not.

        Local and without network. For standalone B, a token changed while getUpdates is held is
        another bot until getMe answers for it, so the earlier identity is not reused.
        """
        if self.is_owner_bot:
            with self._facts_lock:
                if self._run_task is None:
                    return "", BOT_NOT_RUNNING
                if self._feed_enabled and _BOT_ID.fullmatch(self._bot_id):
                    return self._bot_id, ""
                return "", self._feed_reason or BOT_UNCONFIRMED
        token = str(self.token_provider() or "").strip()
        with self._facts_lock:
            if self._run_task is not None and self._standalone_conflict(token):
                return "", BOT_CONFLICT_WITH_OWNER
            return _confirmed(self._run_task is not None, token, self._client_token, self._bot_id)

    def _standalone_conflict(self, token: str) -> bool:
        if token:
            return self._runtime_state == "conflict_with_owner_bot" and token == self._conflict_token
        identity = getattr(self.owner_feed, "connection_identity", None)
        return identity is not None and identity.connector_token_held is True

    def recorded_bot(self) -> str:
        """The bot last confirmed for this profile, whichever process's runtime asked.

        For tools, which also run in pooled workers whose runtime never starts. ``confirmed_bot``
        stays this runtime's own answer, which the status route reports as not running there.
        """
        if self.is_owner_bot:
            with self._facts_lock:
                if self._feed_enabled and _BOT_ID.fullmatch(self._bot_id):
                    return self._bot_id
            if not self.store_exists:
                return ""
            try:
                record = self.store.confirmed_owner_bot()
            except (sqlite3.Error, OSError):
                return ""
            return canonical_bot_id(record.get("account_id") if isinstance(record, dict) else "")
        return self.store.confirmed_account(str(self.token_provider() or "").strip())

    def status_snapshot(self, custody: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The runtime's status facts. The token/account is read once; the bot, its state, the poll attempt and the
        confirmed account (``confirmed_account``/``confirmed_reason``, as confirmed_bot() answers) come from one
        consistent read of the runtime, so the route never pairs one bot's name or result with another's chats."""
        if custody is None:
            custody = self.store.status_snapshot() if (not self.is_owner_bot or self.store_exists) else {}
        if self.is_owner_bot:
            with self._facts_lock:
                state, label, attempt = self._runtime_state, self._bot_label, self._poll_attempt
                feed_enabled, feed_reason, invite_url = self._feed_enabled, self._feed_reason, self._invite_url
                if self._run_task is None:
                    account, reason = "", BOT_NOT_RUNNING
                elif feed_enabled and _BOT_ID.fullmatch(self._bot_id):
                    account, reason = self._bot_id, ""
                else:
                    account, reason = "", feed_reason or BOT_UNCONFIRMED
                identity = getattr(self.submitter, "connector_identity", {})
            error_text, error_at, error_source = self._last_error_record
            host = self._effective_host_record()
            return {
                "runtime_state": state,
                "bot_label": label,
                "confirmed_account": account,
                "confirmed_reason": reason,
                "feed_enabled": feed_enabled,
                "feed_reason": feed_reason,
                "invite_url": invite_url,
                **({"identity": identity} if account and identity else {}),
                "last_error": error_text,
                "delivery_reporting": host["status"] or "unsupported",
                "presence_continuation_version": host["continuation_version"],
                "reaction_context": "available" if host["reaction_context"] == 1 else "update_required",
                "reaction_history": "available" if host["explicit_completion"] == 1 else "update_required",
                "last_event_at": custody.get("last_event_at", ""),
                "last_delivery_at": custody.get("last_delivery_at", ""),
                "last_error_at": _iso(error_at),
                "last_error_source": error_source,
                "provider_ok_at": _iso(self._provider_ok_at),
                "host_checked": host["status"] in {"supported", "unsupported"},
                "host_checked_at": _iso(host["checked_at"] or 0.0),
                "host_unasked": host["unasked"],
                "places_version": host["places_version"],
                **_poll_facts(attempt, account),
            }
        try:
            token = str(self.token_provider() or "").strip()
        except Exception:
            token = ""
        with self._facts_lock:
            state, label, attempt = self._runtime_state, self._bot_label, self._poll_attempt
            wait = self._ready_wait
            if self._run_task is not None and self._standalone_conflict(token):
                state = "conflict_with_owner_bot"
                account, reason = "", BOT_CONFLICT_WITH_OWNER
            else:
                if state == "conflict_with_owner_bot":
                    state = "not_started"
                account, reason = _confirmed(self._run_task is not None, token, self._client_token, self._bot_id)
            identity = getattr(self.submitter, "connector_identity", {})
        error_text, error_at, error_source = self._last_error_record  # one record, read once
        host = _host_record(self.submitter)  # one completed /identity check, read once
        return {
            "runtime_state": state,
            "bot_label": label,
            "confirmed_account": account,
            "confirmed_reason": reason,
            "ready_wait": wait,
            **({"identity": identity} if host["identity"] == 1 and identity else {}),
            "last_error": error_text,
            "delivery_reporting": host["status"] or "unsupported",
            # 2: early custody answers; 0: each turn waits for its terminal (older Host).
            "presence_continuation_version": host["continuation_version"],
            "reaction_context": "available" if host["reaction_context"] == 1 else "update_required",
            "reaction_history": "available" if host["explicit_completion"] == 1 else "update_required",
            "last_event_at": custody.get("last_event_at", ""),
            "last_delivery_at": custody.get("last_delivery_at", ""),
            # Additive card facts: when the newest error was recorded and by which loop, the last getUpdates answer,
            # and whether Ouroboros answered /identity yet (until then capabilities are unknown, not missing).
            "last_error_at": _iso(error_at),
            "last_error_source": error_source,
            "provider_ok_at": _iso(self._provider_ok_at),
            "host_checked": host["status"] in {"supported", "unsupported"},
            # When that check last ended, answered or not ("" before any): its facts are a past check, not live.
            "host_checked_at": _iso(host["checked_at"] or 0.0),
            # Why that check made no request ("" when it made one): Ouroboros was not asked, not silent.
            "host_unasked": host["unasked"],
            "places_version": host["places_version"],
            **_poll_facts(attempt, token),
        }

    async def owner_status_snapshot(self, requested_account_id: str = "") -> Dict[str, Any]:
        """Focused UI status for ``GET /api/extensions/telegram-bot/owner_bot_status[?account_id=<A>]``.

        Schema: ``{v: 1, enabled: bool|null, reason: str, connect: []|rows|null,
        account_id?, identity?: {sends_as: {id, label, kind: "bot"}}, invite_url?}``.
        ``account_id``, ``identity`` and ``invite_url`` appear ONLY when verified current A is ready
        and matches any supplied query. Never returns standalone B.
        """
        raw_req = str(requested_account_id or "").strip()
        req_id = canonical_bot_id(raw_req) if raw_req else ""
        if raw_req and not req_id:
            return {"v": 1, "enabled": None, "reason": "account_mismatch", "connect": None}

        with self._facts_lock:
            running = self._run_task is not None
            state = self._runtime_state
            observed = self._feed_observed
            feed_enabled = self._feed_enabled
            feed_reason = self._feed_reason
            bot_id = self._bot_id
            identity_ready = isinstance(self._owner_identity, dict) and isinstance(self._owner_identity.get("sends_as"), dict)
            off_account_id = self._off_account_id

        if not running:
            reason = "stopped" if state == "stopped" else "not_started"
            return {"v": 1, "enabled": None, "reason": reason, "connect": None}
        if state == "unsupported":
            return {"v": 1, "enabled": None, "reason": "unsupported", "connect": None}
        if not observed:
            return {"v": 1, "enabled": None, "reason": feed_reason or "not_started", "connect": None}

        if feed_enabled and not feed_reason and _BOT_ID.fullmatch(bot_id) and identity_ready:
            if req_id and req_id != bot_id:
                return {"v": 1, "enabled": None, "reason": "account_mismatch", "connect": None}
            # The native bridge owns membership for the current consent period. Hub sightings are title hints,
            # never permission to offer a group: Off/On can have happened entirely between our poll ticks.
            try:
                current = await self.owner_feed.identity(groups=True)
            except OwnerFeedError:
                return {"v": 1, "enabled": None, "reason": "host_unavailable", "connect": None}
            if not current.confirmed:
                reason = current.reason or "identity_unconfirmed"
                if reason == "feed_off":
                    if req_id and req_id != current.account_id:
                        reason = "account_mismatch"
                    else:
                        return {"v": 1, "enabled": False, "reason": reason, "connect": None}
                return {"v": 1, "enabled": None, "reason": reason, "connect": None}
            if current.account_id != bot_id:
                return {"v": 1, "enabled": None, "reason": "account_mismatch", "connect": None}
            groups = None
            if current.group_ids is not None:
                groups = await asyncio.to_thread(self._label_owner_groups, bot_id, current.group_ids)
            if self._stopping or self.confirmed_bot()[0] != bot_id:
                return {"v": 1, "enabled": None, "reason": "identity_unconfirmed", "connect": None}
            return {
                "v": 1,
                "enabled": True,
                "reason": "",
                "account_id": bot_id,
                "identity": current.identity,
                "connect": groups,
                **({"invite_url": current.invite_url} if current.invite_url else {}),
            }

        if not feed_enabled and feed_reason == "feed_off":
            if req_id and (not off_account_id or req_id != off_account_id):
                return {"v": 1, "enabled": None, "reason": "account_mismatch", "connect": None}
            return {"v": 1, "enabled": False, "reason": "feed_off", "connect": None}

        return {"v": 1, "enabled": None, "reason": feed_reason or "unconfigured", "connect": None}

    def _label_owner_groups(self, account: str, ids: Tuple[str, ...]) -> List[Dict[str, str]]:
        labels = {}
        if ids and self.store_exists:
            try:
                labels = {row["conversation_id"]: row["label"] for row in self.store.offered_groups(account, group_only=True)}
            except (sqlite3.Error, OSError):
                pass  # Membership is known independently; only the saved titles are unavailable.
        return sorted(({"account_id": account, "conversation_id": group, "label": labels.get(group, f"Group ({group})")}
                       for group in ids), key=lambda row: (row["label"].casefold(), row["conversation_id"]))

    def _bound_owner_client(self, client: TelegramClient, binding: Dict[str, Any]) -> TelegramClient:
        """Bind a relay ``TelegramClient`` to one immutable place sideband (no shared mutable state)."""
        if not self.is_owner_bot:
            return client
        transport = getattr(client, "_transport", None)
        with_binding = getattr(transport, "with_binding", None)
        if not callable(with_binding):
            raise RuntimeError("owner-bot relay transport does not support place binding")
        return TelegramClient(
            client._token,
            transport=with_binding(binding),
            api_server=client._api_server,
        )

    def _effective_host_record(self) -> Dict[str, Any]:
        host = _host_record(self.submitter)
        if host["status"] in {"supported", "unsupported"} or not callable(self._capability_source):
            return host
        source = self._capability_source()
        return _host_record(source) if source is not None else host

    def _poll_done(self, token: str, result: str, error: str = "", failure: str = "", stage: str = "") -> None:
        with self._facts_lock:
            if result == "failed":
                self._runtime_state = "provider_error"
            self._poll_attempt = {"token": token, "result": result, "at": time.time(), "error": error,
                                  "failure": failure, "stage": stage if result == "failed" else ""}

    def _commit_raw_update(
        self,
        update: Dict[str, Any],
        *,
        account: str,
        username: str,
        download_limit: int,
        group_only: bool,
        advance_offset: bool = True,
        feed_cursor: Optional[Tuple[str, int]] = None,
        sighting_order: Optional[int] = None,
        feed_place: Any = None,
    ) -> None:
        """One shared intake path for both standalone B and owner-bot group feed A."""
        update_id = _update_id(update)
        if update_id is None:
            if feed_cursor is not None:
                self.store.commit_update(
                    sighting_order if sighting_order is not None else 0,
                    None,
                    (),
                    advance_offset=False,
                    feed_cursor=feed_cursor,
                    sighting_order=sighting_order,
                )
            return
        settings = load_settings(self._settings_dir)
        event = parse_telegram_update(
            update,
            bot_account_id=account,
            management_group_id="" if self.is_owner_bot else str(settings.get("management_group_id") or "").strip(),
            places=bool(getattr(self.submitter, "places_version", 0)),
            bot_username=username,
            words=settings.get("words", []),
            download_limit=download_limit,
            group_only=group_only,
        )
        place_binding: Optional[Dict[str, Any]] = None
        if self.is_owner_bot and event is not None:
            reaction = event.message.get("event_kind") in _REACTION_KINDS
            place_binding = feed_item_place_binding(
                feed_place,
                account_id=account,
                conversation_id=event.conversation_id,
                thread_id=None if reaction else event.thread_id,
            )
            if place_binding is None:
                # Wire v2: raw feed events without a valid exact place binding never enter inbox.
                event = None
            elif reaction and place_binding["thread_id"]:
                event = event.in_thread(place_binding["thread_id"])
        # A membership or service update notes its group and moves the offset only. The owner bot's store can hold
        # several of the owner's bots, whose Telegram update numbers may coincide: its rows are ordered and kept apart
        # by the feed's own number, while the event's identity stays its source (telegram:<bot>:<update>).
        self.store.commit_update(
            sighting_order if (self.is_owner_bot and sighting_order is not None) else update_id,
            event,
            group_sightings(update, account, group_only=group_only),
            advance_offset=advance_offset,
            feed_cursor=feed_cursor,
            sighting_order=sighting_order,
            place_binding=place_binding,
        )

    async def _discover_feed_capability(self) -> Tuple[str, int, bool]:
        """Query Host /identity capability for the owner-bot group feed (never calls /telegram/feed/*)."""
        source = self._capability_source() if callable(self._capability_source) else None
        target = source if source is not None else self.submitter
        discover = getattr(target, "discover_delivery_support", None)
        if discover:
            await discover()
        if source is not None and source is not self.submitter:
            caps = getattr(source, "capabilities", None)
            if caps is not None and dataclasses.is_dataclass(caps):
                self.submitter.capabilities = caps
            src_transport = getattr(source, "transport", None)
            if src_transport is not None and hasattr(self.submitter, "transport"):
                self.submitter.transport = src_transport
        host = _host_record(target)
        return host["status"], int(host.get("telegram_group_feed", 0) or 0), bool(discover)

    def _clear_owner_identity(
        self,
        *,
        state: str,
        reason: str,
        enabled: bool,
        observed: bool = True,
        off_account_id: str = "",
        invite_url: str = "",
        label: str = "not connected",
        forget_recorded: bool = True,
    ) -> None:
        with self._facts_lock:
            self._client = None
            self._client_token = ""
            self._bot_id = ""
            self._bot_username = ""
            self._bot_label = label or "not connected"
            self._runtime_state = state
            self._feed_observed = observed
            self._feed_enabled = enabled
            self._feed_reason = reason
            self._off_account_id = canonical_bot_id(off_account_id)
            self._owner_identity = None
            self._invite_url = invite_url
            self.submitter.connector_identity = {}
        if forget_recorded and self.store_exists:
            try:
                self.store.forget_confirmed_owner_bot()
            except (sqlite3.Error, OSError) as exc:
                self._log("warning", f"telegram-bot: could not clear recorded owner bot: {_error(exc)}")

    async def _feed_loop(self, workers: List[asyncio.Task[Any]]) -> None:
        """Owner-bot group feed loop (wire v2): never calls getUpdates, deleteWebhook, or getMe."""
        cap_backoff = 1.0
        while not self._stopping:
            status, feed_version, has_discover = await self._discover_feed_capability()
            if feed_version == 1:
                break
            if status in {"supported", "unsupported"} or not has_discover:
                with self._facts_lock:
                    self._runtime_state = "unsupported"
                    self._feed_observed = True
                    self._feed_enabled = False
                    self._feed_reason = "unsupported"
                    self._owner_identity = None
                    self._off_account_id = ""
                if self._stop_event is not None:
                    await self._stop_event.wait()
                return
            with self._facts_lock:
                self._runtime_state = "not_started"
                self._feed_observed = False
                self._feed_enabled = False
                self._feed_reason = "host_unavailable"
            await self._pause(cap_backoff)
            cap_backoff = min(10.0, cap_backoff * 1.5)

        if self._stopping:
            return
        if self.store_exists:
            self._start_workers(workers)

        backoff = 2.0
        while not self._stopping:
            stage = "identity"
            account = self._bot_id
            try:
                if self.owner_feed is None:
                    self._clear_owner_identity(
                        state="feed_off",
                        reason="owner_feed_unconfigured",
                        enabled=False,
                        observed=False,
                        forget_recorded=True,
                    )
                    await self._pause(5.0)
                    continue
                info = await self.owner_feed.identity()
                if not info.confirmed:
                    is_explicit_off = (not info.enabled) and info.reason == "feed_off"
                    state = "feed_off" if is_explicit_off else "waiting_for_owner_bot"
                    reason = info.reason or ("identity_unconfirmed" if info.enabled else "feed_off")
                    self._clear_owner_identity(
                        state=state,
                        reason=reason,
                        enabled=False,
                        observed=True,
                        off_account_id=info.account_id if is_explicit_off else "",
                        label=info.label or "not connected",
                        forget_recorded=is_explicit_off,
                    )
                    await self._pause(5.0)
                    backoff = 2.0
                    continue
                account = info.account_id
                client = self._activate_owner_client(info)
                self._start_workers(workers)
                stage = "updates"
                with self._facts_lock:
                    self._runtime_state = "polling"
                items = await self.owner_feed.updates(
                    account,
                    offset=self.store.owner_feed_offset(account),
                    timeout_sec=25,
                )
                self._provider_ok_at = time.time()
                stage = "record"
                for item in items:
                    self._commit_feed_item(item, account=account, username=info.username,
                                           download_limit=client.download_limit)
                self._poll_done(account, "ok")
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except RelayRefusalError as exc:
                self._last_error = _error(exc)
                if exc.relay_refusal == "feed_off":
                    self._clear_owner_identity(
                        state="feed_off",
                        reason="feed_off",
                        enabled=False,
                        observed=True,
                        off_account_id=account,
                        forget_recorded=True,
                    )
                elif exc.relay_refusal == "busy":
                    await self._pause(_relay_hold_retry_after(exc))
                    continue
                elif exc.relay_refusal in {
                    "account_mismatch",
                    "bridge_not_ready",
                    "auth_unavailable",
                    "feed_unavailable",
                    "auth_blocked",
                    "connector_mismatch",
                }:
                    self._clear_owner_identity(
                        state="waiting_for_owner_bot",
                        reason=exc.relay_refusal,
                        enabled=False,
                        observed=True,
                        forget_recorded=False,
                    )
                else:
                    # The feed is read from Ouroboros on this computer, never from Telegram: its refusal is local.
                    self._poll_done(account, "failed", self._last_error, "local", stage)
                await self._pause(_relay_hold_retry_after(exc))
            except Exception as exc:
                self._last_error = _error(exc)
                if stage == "identity":
                    self._clear_owner_identity(
                        state="provider_error",
                        reason="host_unavailable",
                        enabled=False,
                        observed=False,
                        forget_recorded=False,
                    )
                self._poll_done(account, "failed", self._last_error, "local", stage)
                self._log("warning", f"telegram-bot: owner-bot group feed poll failed: {self._last_error}")
                await self._pause(backoff)
                backoff = min(60.0, backoff * 1.7)

    def _activate_owner_client(self, info: Any) -> TelegramClient:
        account = info.account_id
        identity_block = (
            {"sends_as": dict(info.identity["sends_as"])}
            if isinstance(getattr(info, "identity", None), dict)
            and isinstance(info.identity.get("sends_as"), dict)
            else {"sends_as": {"id": account, "label": info.label or (f"@{info.username}" if info.username else account), "kind": "bot"}}
        )
        label = str(identity_block["sends_as"].get("label") or "").strip() or account
        with self._facts_lock:
            existing = self._client if self._bot_id == account else None
        if existing is not None:
            with self._facts_lock:
                self._bot_username = info.username
                self._bot_label = label
                self._feed_observed = True
                self._feed_enabled = True
                self._feed_reason = ""
                self._off_account_id = ""
                self._owner_identity = identity_block
                self._invite_url = info.invite_url
                self.submitter.connector_identity = {"sends_as": dict(identity_block["sends_as"])}
            return existing
        if self.relay_transport_factory is not None:
            transport = self.relay_transport_factory(account, self.state_dir)
        else:
            assert self.owner_feed is not None
            transport = self.owner_feed.make_relay_transport(account, self.state_dir)
        client = TelegramClient(
            f"relay-account-{account}",
            transport=transport,
            api_server=RELAY_API_SERVER,
        )
        self.store.record_confirmed_owner_bot(account, username=info.username, label=label)
        with self._facts_lock:
            self._bot_id = account
            self._bot_username = info.username
            self._bot_label = label
            self._feed_observed = True
            self._feed_enabled = True
            self._feed_reason = ""
            self._off_account_id = ""
            self._owner_identity = identity_block
            self._invite_url = info.invite_url
            self.submitter.connector_identity = {"sends_as": dict(identity_block["sends_as"])}
            self._client = client
            self._client_token = account
        self._last_error = ""
        return client

    def _commit_feed_item(
        self,
        item: Dict[str, Any],
        *,
        account: str,
        username: str,
        download_limit: int,
    ) -> None:
        feed_id = _nonneg_int(item.get("feed_id"))
        if feed_id is None:
            return
        raw_account = item.get("account_id")
        if raw_account not in (None, "") and canonical_bot_id(raw_account) != account:
            return
        cursor = (account, feed_id + 1)
        update = item.get("update")
        if isinstance(update, dict):
            self._commit_raw_update(
                update,
                account=account,
                username=username,
                download_limit=download_limit,
                group_only=True,
                advance_offset=False,
                feed_cursor=cursor,
                sighting_order=feed_id,
                feed_place=item.get("place"),
            )
            return
        sighting = item.get("sighting")
        if isinstance(sighting, dict):
            self.store.commit_update(
                feed_id,
                None,
                feed_sighting(sighting, account),
                advance_offset=False,
                feed_cursor=cursor,
                sighting_order=feed_id,
            )
            return
        self.store.commit_update(
            feed_id,
            None,
            (),
            advance_offset=False,
            feed_cursor=cursor,
            sighting_order=feed_id,
        )

    def _record_owner_conflict(self, bot_id: str, *, token: str, label: str = "") -> None:
        try:
            self.store.forget_confirmed_bot(token)
        except sqlite3.Error as cleanup:
            self._log("warning", f"telegram-bot: could not forget conflicting standalone bot: {_error(cleanup)}")
        with self._facts_lock:
            self._runtime_state = "conflict_with_owner_bot"
            self._conflict_token = token
            self._conflict_bot_id = canonical_bot_id(bot_id)
            self._bot_label = label or self._bot_label or bot_id
            self._bot_id = ""
            self._bot_username = ""
            self._client = None
            self._client_token = ""
            self.submitter.connector_identity = {}
        self._last_error = BOT_CONFLICT_WITH_OWNER

    async def _claim_b_poll_ownership(self, bot_id: str, *, token: str, label: str = "") -> str:
        """Claim exclusive standalone B poll ownership via ``POST /telegram/feed/poller``.

        Used whenever the Host advertises ``telegram_group_poll_claim: 1``. Never falls back to
        observational ``GET /telegram/feed/identity`` prechecks when the POST fails or returns an
        unknown/mismatch answer.
        """
        if self.owner_feed is None or not bot_id:
            return "wait"
        claim = await self.owner_feed.claim_poller(bot_id)
        if claim.status == "conflict":
            self._record_owner_conflict(bot_id, token=token, label=label)
            return "conflict"
        if claim.status == "account_changed" or (
            claim.status == "owned" and str(self.token_provider() or "").strip() != token
        ):
            with self._facts_lock:
                if self._client_token == token:
                    self._client = None
                    self._client_token = ""
                    self._bot_id = ""
                    self._bot_username = ""
                    self.submitter.connector_identity = {}
                if self._conflict_token == token:
                    self._conflict_token = ""
                    self._conflict_bot_id = ""
                if self._client is None and self._runtime_state != "waiting_for_token":
                    self._runtime_state = "not_started"
            self._last_error = "Standalone bot token changed or is unavailable; waiting to reconfirm"
            return "wait"
        if claim.status == "owned":
            with self._facts_lock:
                if self._conflict_token == token:
                    self._conflict_token = ""
                    self._conflict_bot_id = ""
            return "clear"
        with self._facts_lock:
            if self._client is None and self._runtime_state != "waiting_for_token":
                self._runtime_state = "not_started"
        self._last_error = claim.error or f"Poller ownership claim unavailable ({claim.code or 'unknown'})"
        return "wait"

    async def _check_owner_collision(self, bot_id: str, *, token: str) -> str:
        """Guard standalone B from polling or deleting the webhook of native owner bot A.

        Used on cores advertising ``telegram_group_feed: 1`` without ``telegram_group_poll_claim: 1``.
        Returns ``"clear"`` when confirmed distinct from native A (``same_as_owner_bridge is False``),
        ``"conflict"`` when the same bot (``True``, even while A's group feed is off), or ``"wait"``
        while unknown (``None``).
        """
        if self.owner_feed is None or not bot_id:
            return "clear"
        info = await self.owner_feed.identity(bot_id=bot_id)
        if info.same_as_owner_bridge is True:
            self._record_owner_conflict(bot_id, token=token, label=info.label or bot_id)
            return "conflict"
        if info.same_as_owner_bridge is False:
            with self._facts_lock:
                if self._conflict_token == token:
                    self._conflict_token = ""
                    self._conflict_bot_id = ""
            return "clear"
        # same_as_owner_bridge is None (unknown / startup race): wait and do not touch Telegram yet.
        with self._facts_lock:
            if self._client is None:
                self._runtime_state = "not_started"
        return "wait"

    async def _poll_loop(self) -> None:
        backoff = 2.0
        while not self._stopping:
            # The token this attempt runs with is the one _ready_client picks under the client lock (``picked``);
            # its result counts only while that token stays configured. ``stage`` says where a failure stopped.
            token, picked, stage = str(self.token_provider() or "").strip(), {}, "identity"
            try:
                if token and self.owner_feed is not None:
                    discover = getattr(self.submitter, "discover_delivery_support", None)
                    if discover:
                        await discover()
                client = await self._ready_client(picked)
                if client is None:
                    await self._pause(5.0)
                    continue
                token, stage = picked.get("token", self._client_token), "updates"
                # The bot this client answers for, even if another worker confirms a new token meanwhile.
                with self._facts_lock:
                    account, username, client_token = self._bot_id, self._bot_username, self._client_token
                if not token or token != client_token or str(self.token_provider() or "").strip() != token:
                    await self._pause(0.05)
                    continue
                discover = getattr(self.submitter, "discover_delivery_support", None)
                if discover:
                    await discover()
                host = _host_record(self.submitter)
                if self.owner_feed is not None and host.get("telegram_group_poll_claim", 0) == 1:
                    if not _BOT_ID.fullmatch(account):
                        await self._pause(5.0)
                        continue
                    if await self._claim_b_poll_ownership(account, token=token) != "clear":
                        await self._pause(5.0)
                        continue
                elif (
                    self.owner_feed is not None
                    and host.get("telegram_group_feed", 0) == 1
                    and _BOT_ID.fullmatch(account)
                ):
                    if await self._check_owner_collision(account, token=token) != "clear":
                        await self._pause(5.0)
                        continue
                self._runtime_state = "polling"
                updates = await client.get_updates(
                    offset=self.store.telegram_offset(), timeout_sec=25
                )
                self._provider_ok_at = time.time()
                stage = "record"
                for update in updates:
                    self._commit_raw_update(
                        update,
                        account=account,
                        username=username,
                        download_limit=client.download_limit,
                        group_only=False,
                        advance_offset=True,
                    )
                self._poll_done(token, "ok")
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                token = picked.get("token", token)  # a refusal of the token picked under the lock is that token's
                self._last_error = _error(exc)
                # The state and this attempt's result change together (one read of them never splits).
                self._poll_done(token, "failed", self._last_error, _poll_failure(exc, stage), stage)
                self._log(
                    "warning", f"telegram-bot: provider poll failed: {self._last_error}"
                )
                await self._pause(backoff)
                backoff = min(60.0, backoff * 1.7)

    async def _ready_client(self, picked: Optional[Dict[str, Any]] = None) -> Optional[TelegramClient]:
        """The client for the token configured now, confirming it with getMe once per token.

        For the owner-bot group feed, returns the already-activated relay client (never reads a token
        or calls deleteWebhook/getMe).
        """
        if self.is_owner_bot:
            with self._facts_lock:
                return self._client
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            token = str(self.token_provider() or "").strip()
            discover = getattr(self.submitter, "discover_delivery_support", None) if self.owner_feed else None
            if picked is not None and token:
                picked["token"] = token
            if not token:
                if discover:
                    await discover()
                    if str(self.token_provider() or "").strip():
                        return self._wait(_WAIT_TOKEN_CHANGED)
                host = _host_record(self.submitter)
                identity = getattr(self.owner_feed, "connection_identity", None)
                held = identity is not None and identity.connector_token_held is True
                waiting = bool(discover and (host["status"] not in {"supported", "unsupported"}
                               or (host.get("telegram_group_feed") == 1
                                   and (identity is None or identity.reason == "auth_unavailable"))))
                with self._facts_lock:
                    self._runtime_state = ("conflict_with_owner_bot" if held else
                                           "not_started" if waiting else "waiting_for_token")
                    self._bot_label = "not configured"
                    self._client = None
                    self._client_token = ""
                    self._conflict_token = ""
                    self._conflict_bot_id = ""
                    self._ready_wait = _WAIT_CONFLICT if held else _WAIT_FOR_HOST if waiting else ""
                return None
            if self._client is not None and token == self._client_token:
                self._ready_wait = ""
                return self._client
            if discover:
                await discover()
            host = _host_record(self.submitter)
            if discover and host["status"] not in {"supported", "unsupported"}:
                # No answer from Ouroboros is not an older Ouroboros: until it answers, this token's bot may be the one
                # its own Telegram bridge reads (and the owner's Panic with it), so Telegram is not contacted with it.
                self._ready_wait = _WAIT_FOR_HOST
                with self._facts_lock:
                    if self._client is None:
                        self._runtime_state = "not_started"
                return None
            claim_supported = self.owner_feed is not None and host.get("telegram_group_poll_claim", 0) == 1
            feed_supported = (
                (not claim_supported)
                and self.owner_feed is not None
                and host.get("telegram_group_feed", 0) == 1
            )
            if claim_supported:
                with self._facts_lock:
                    steady_conflict = (
                        self._runtime_state == "conflict_with_owner_bot"
                        and token == self._conflict_token
                        and bool(self._conflict_bot_id)
                    )
                    conflict_bot_id = self._conflict_bot_id
                    conflict_label = self._bot_label
                if steady_conflict:
                    claimed = await self._claim_b_poll_ownership(conflict_bot_id, token=token, label=conflict_label)
                    if claimed != "clear":
                        return self._wait(claimed)
            prefix = token_bot_id_prefix(token) if feed_supported else ""
            if feed_supported and prefix:
                checked = await self._check_owner_collision(prefix, token=token)
                if checked != "clear":
                    return self._wait(checked)
            client = self.client_factory(token)
            try:
                if claim_supported:
                    identity = await client.get_me()
                    checked_id = canonical_bot_id(identity.get("id"))
                    checked_user = str(identity.get("username") or "").strip()
                    checked_label = f"@{checked_user}" if checked_user else checked_id
                    if not checked_id:
                        self.store.forget_confirmed_bot(token)
                        self._last_error = "Telegram getMe did not return a canonical bot id"
                        return self._wait(_WAIT_UNNAMED)
                    if str(self.token_provider() or "").strip() != token:
                        return self._wait(_WAIT_TOKEN_CHANGED)
                    claimed = await self._claim_b_poll_ownership(checked_id, token=token, label=checked_label)
                    if claimed != "clear":
                        return self._wait(claimed)
                    await client.delete_webhook()
                elif feed_supported:
                    # Confirm identity before deleteWebhook so a token matching native owner bot A
                    # can never mutate A's webhook even if its prefix did not name its bot id.
                    identity = await client.get_me()
                    checked_id = canonical_bot_id(identity.get("id"))
                    if checked_id and checked_id != prefix:
                        checked = await self._check_owner_collision(checked_id, token=token)
                        if checked != "clear":
                            return self._wait(checked)
                    await client.delete_webhook()
                else:
                    await client.delete_webhook()
                    identity = await client.get_me()
            except TelegramApiError as exc:
                if exc.error_code in _TOKEN_REFUSED:
                    # Telegram's refusal is this attempt's outcome. Failing to forget the bot recorded for the token
                    # here (a locked store) is recorded apart and never replaces what Telegram said.
                    try:
                        self.store.forget_confirmed_bot(token)
                    except sqlite3.Error as cleanup:
                        self._log("warning", "telegram-bot: could not forget the bot recorded for a refused token: "
                                  f"{_error(cleanup)}")
                raise
            bot_id = str(identity.get("id") or "unknown")
            # Before anything in memory changes: if the store refuses, the next call asks getMe again.
            if _BOT_ID.fullmatch(bot_id):
                self.store.record_confirmed_bot(token, bot_id)
            else:
                self.store.forget_confirmed_bot(token)
            username = str(identity.get("username") or "").strip()
            with self._facts_lock:  # the new bot's id, name and token become visible together
                self._bot_id = bot_id
                self._bot_username = username
                self._bot_label = f"@{username}" if username else self._bot_id
                self.submitter.connector_identity = {"sends_as": {
                    "id": self._bot_id, "label": self._bot_label, "kind": "bot"}}
                self._client = client
                self._client_token = token
                self._conflict_token = ""
                self._conflict_bot_id = ""
                self._ready_wait = ""
            self._last_error = ""
            return client

    def _wait(self, reason: str) -> None:
        """No client now, for a reason that ends by itself or by the owner's change (not a missing token): a worker
        holds its row with these words and uses no attempt (``_unready``)."""
        self._ready_wait = {"conflict": _WAIT_CONFLICT, "wait": _WAIT_FOR_CLAIM}.get(reason, reason)
        return None

    def _unready(self) -> Exception:
        """What a worker raises when ``_ready_client`` gave no client: a hold for the owner bot's inactive feed or an
        arbitration wait (no attempt used), else the missing token, which uses an attempt as 0.13.1 did."""
        if self.is_owner_bot:
            return PlaceHeld(HoldNote("Held: Owner bot group feed is not active.", ""))
        if self._ready_wait:
            return PlaceHeld(HoldNote(f"Held: {self._ready_wait}", ""))
        return RuntimeError("telegram token is unavailable")

    def _pinned_account(self, payload: Dict[str, Any]) -> str:
        explicit = canonical_bot_id(payload.get("account_id"))
        if explicit:
            return explicit
        reporting = payload.get("_reporting") if isinstance(payload.get("_reporting"), dict) else {}
        origin = reporting.get("origin")
        # 0.13.1 automatic replies kept the event's confirmed bot inside their reporting record. An unpinned
        # proactive tool row could instead acquire a reporting account only when an attempt ran: not its target.
        if self.is_owner_bot or (isinstance(origin, dict) and origin.get("kind") == "automatic"):
            return canonical_bot_id(reporting.get("account_id"))
        return ""

    def _ensure_pinned_account(self, payload: Dict[str, Any]) -> str:
        """The bot a row is sent as. Called once a client for the confirmed bot is ready, so a row pinned to another
        bot was queued for one that is not this profile's now: it ends unsent, never retargeted to this one."""
        pinned = self._pinned_account(payload)
        active = canonical_bot_id(self._bot_id)
        if pinned and active and pinned != active:
            raise RetiredTarget(f"Not sent: it was queued for bot {pinned}, and this connection's bot is now {active}; "
                                "it is never sent through another bot.")
        if self.is_owner_bot and not active:
            raise PlaceHeld(HoldNote("Held: Owner bot group feed is not active.", ""))
        return pinned or active or self._bot_id

    def _owner_place_binding(self, payload: Dict[str, Any], *, thread_id: str = "") -> Optional[Dict[str, Any]]:
        """The retained place binding of an owner-bot row, for its own bot and topic (never rebound)."""
        if not self.is_owner_bot:
            return None
        binding = normalize_place_binding(
            payload.get("_place_binding"),
            account_id=self._pinned_account(payload),
            conversation_id=str(payload.get("chat_id") or "").strip(),
            thread_id=str(thread_id or "").strip(),
        )
        if binding is None:
            raise RetiredTarget("Not sent: its place connection is missing or does not match it; it is never bound "
                                "to another connection.")
        return binding

    async def _inbox_worker(self) -> None:
        while not self._stopping:
            worked = await self.process_one_inbox()
            if not worked:
                await self._pause(0.5)

    def _owner_inbox_ready(self, lease: InboxLease, account_id: str) -> bool:
        """Keep the same event waiting for its own feed, or settle it once another bot is confirmed."""
        with self._facts_lock:
            active = canonical_bot_id(self._bot_id) if self._feed_enabled else ""
        if not active:
            self.store.defer_inbox(lease.event_id, reason=str(self._unready()), retry_after_sec=5.0)
            return False
        if active != account_id:
            self.store.mark_inbox_failed(lease.event_id, reason=OWNER_BOT_CHANGED)
            self._log("info", f"telegram-bot: an update of bot {account_id} was not passed on; "
                              f"the bot connected for groups is now {active}")
            return False
        return True

    async def process_one_inbox(self) -> bool:
        # A Host turn can run for up to 30 minutes (version 2 answers once its
        # author takes mail); keep one local claimant through that bounded
        # request so another worker cannot resubmit it.
        lease = self.store.claim_inbox(lease_sec=1860.0)
        if lease is None:
            return False
        owner_binding: Optional[Dict[str, Any]] = None
        if self.is_owner_bot:
            owner_binding = normalize_place_binding(
                lease.place_binding,
                account_id=str(lease.payload.get("account_id") or ""),
                conversation_id=str(lease.payload.get("conversation_id") or ""),
                thread_id=str(lease.payload.get("thread_id") or ""),
            )
            if owner_binding is None:
                self.store.mark_inbox_failed(lease.event_id, reason="missing_owner_place_binding")
                return True
            if not self._owner_inbox_ready(lease, owner_binding["account_id"]):
                return True
        if str((lease.payload.get("message") or {}).get("event_kind") or "") in _REACTION_KINDS:
            deliverable = await self._reaction_deliverable(lease.payload)
            if owner_binding is not None and not self._owner_inbox_ready(lease, owner_binding["account_id"]):
                return True
            if not deliverable:
                # Retained here as a terminal row, whatever a later Host can do.
                self.store.mark_inbox_failed(lease.event_id, reason=REACTION_WITHHELD)
                return True
        staged: List[pathlib.Path] = []
        # Where this attempt stops if it fails, recorded with the row (CustodyStore.release_inbox): the status says
        # what the update waits for, never a failed handoff when Telegram's download failed.
        stage = "download"
        try:
            staged = (
                await self._stage_attachments(lease, place_binding=owner_binding)
                if owner_binding is not None
                else await self._stage_attachments(lease)
            )
            stage = "submit"
            try:
                if owner_binding is not None:
                    # The feed might have changed while waiting for a file. An already staged file stays in custody.
                    if not self._owner_inbox_ready(lease, owner_binding["account_id"]):
                        return True
                    result = await self.submitter.submit(
                        lease.payload,
                        staged,
                        binding_id=owner_binding["place_id"],
                        connected_revision=owner_binding["connected_revision"],
                    )
                else:
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
                stage = ""  # Ouroboros answered; a failure from here on is this computer's
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
            # HTTP status, keep the event first in its conversation with backoff, as does a
            # download that may still pass (Telegram's flood wait is honoured as a minimum).
            wait = getattr(exc, "retry_after", None) or 0.0
            self.store.release_inbox(
                lease.event_id,
                reason=_error(exc),
                retry_after_sec=max(_retry_delay(lease.attempts), wait),
                stage=stage,
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

    async def _stage_attachments(
        self,
        lease: InboxLease,
        *,
        place_binding: Optional[Dict[str, Any]] = None,
    ) -> List[pathlib.Path]:
        """Download each attachment once, or settle why it is not a file; only staged files reach the Host.

        A settled record (too_large, failed, unsupported) keeps its file_id, and its note says why in one factual
        sentence (core contract). It is stored before the event is submitted, so every later attempt submits the
        same event. A download that may still pass keeps the event first in its conversation until
        ``_bounded_failure`` settles it, and the event waits the longest delay Telegram named. After a flood wait
        (429) no further file is asked for; the files already fetched stay in staging for the next attempt.
        """
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
            raise self._unready()
        if self.is_owner_bot:
            event_account = canonical_bot_id(lease.payload.get("account_id"))
            active_account = canonical_bot_id(self._bot_id)
            if event_account and active_account and event_account != active_account:
                raise RelayRefusalError(
                    "getFile",
                    f"Event belongs to account {event_account}, active owner bot is {active_account}",
                    relay_refusal="account_mismatch",
                )
            binding = place_binding or normalize_place_binding(
                lease.place_binding,
                account_id=event_account,
                conversation_id=str(lease.payload.get("conversation_id") or ""),
                thread_id=str(lease.payload.get("thread_id") or ""),
            )
            if binding is None:
                raise RelayRefusalError(
                    "getFile",
                    "Retained place binding is missing for attachment download",
                    relay_refusal="binding_changed",
                )
            client = self._bound_owner_client(client, binding)
        limit = client.download_limit
        event_dir = self.state_dir / "staging" / _safe_component(lease.event_id)
        staged, settled, waiting = [], False, None
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            if attachment.get("status") in _NOT_STAGED or not attachment.get("file_id"):
                continue
            # A row stored before photos and documents carried the mark (0.10.5 and earlier): the same fact.
            marks = too_large_marks(_declared_size(attachment), limit)
            if marks:
                attachment.update(marks)
                settled = True
                continue
            name = _safe_filename(
                str(attachment.get("file_name") or f"attachment-{index}")
            )
            destination = event_dir / f"{index}-{name}"
            if not destination.is_file() or destination.stat().st_size == 0:
                try:
                    await client.download_file(str(attachment["file_id"]), destination)
                except TelegramFileTooLarge as exc:
                    attachment.update(status="too_large", note=too_large_note(
                        _declared_size(attachment), limit, exc.description if exc.error_code else ""))
                    settled = True
                    continue
                except TelegramFileUnavailable as exc:
                    attachment.update(status="failed", note=f'Telegram answered "{exc.description}"')
                    settled = True
                    continue
                except RelayRefusalError as exc:
                    if exc.relay_refusal in _RELAY_DOWNLOAD_TERMINAL_REFUSALS:
                        attachment.update(status="failed", note=f'Relay refused download ({exc.relay_refusal})')
                        settled = True
                        continue
                    note = self._bounded_failure(lease.event_id, index, exc)
                    if note is None:
                        if waiting is None or (exc.retry_after or 0) > (waiting.retry_after or 0):
                            waiting = exc
                        break
                    attachment.update(status="failed", note=note)
                    settled = True
                    continue
                except TelegramApiError as exc:
                    note = self._bounded_failure(lease.event_id, index, exc)
                    if note is None:
                        if waiting is None or (exc.retry_after or 0) > (waiting.retry_after or 0):
                            waiting = exc
                        if exc.error_code == 429:
                            break  # Telegram asked this bot to wait: nothing more is asked of it now
                        continue  # the other files are still fetched now
                    attachment.update(status="failed", note=note)
                    settled = True
                    continue
            attachment["status"] = "staged"
            staged.append(destination)
        if settled:
            self.store.checkpoint_inbox(lease.event_id, lease.payload)
        if waiting is not None:
            raise waiting
        self.store.forget_download_failures(lease.event_id)
        return staged

    def _bounded_failure(self, event_id: str, ordinal: int, exc: TelegramApiError) -> Optional[str]:
        """The note of this attachment's terminal ``failed`` record, or ``None`` while it is still retried.

        Count a Telegram file refusal, a standalone transport failure alongside successful polls, or a relay read
        timeout after getFile in the same attempt. A flood wait, a refused token, a relay pause and a
        network that is down count nothing. It ends after ``_DOWNLOAD_FAILURES_BEFORE_FAILED`` such
        failures and at least ``_DOWNLOAD_FAILING_SECONDS`` after the first of them.
        """
        now = time.time()
        code = exc.error_code
        if isinstance(exc, (RelayRefusalError, TelegramRequestNotSent)):
            counted = False
        elif code in (401, 429) or (code == 404 and exc.endpoint == "getFile"):
            counted = False
        elif code:
            counted = True
        elif self.is_owner_bot:
            # Host feed polls do not prove Telegram is answering. Only the typed wait after getFile counts.
            counted = isinstance(exc, RelayDownloadTimeout)
        else:
            previous = self.store.download_failure(event_id, ordinal)
            since = max(float(previous["last_failed_at"]) if previous else 0.0, now - _POLL_EVIDENCE_SECONDS)
            counted = self._provider_ok_at >= since
        record = self.store.note_download_failure(event_id, ordinal, error=_error(exc), counted=counted, now=now)
        failing = now - float(record["first_failed_at"] or now)
        if not counted or record["own_failures"] < _DOWNLOAD_FAILURES_BEFORE_FAILED or failing < _DOWNLOAD_FAILING_SECONDS:
            return None
        last = exc.description if isinstance(exc, RelayDownloadTimeout) else (
            f'Telegram answered "{exc.description}"' if code else f"no answer ({exc.description})"
        )
        return f"not received after {record['own_failures']} attempts over {failing / 60:.0f} min; last: {last}"

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
        lease = self.store.claim_outbox(lease_sec=120.0, supports_conditional=True)
        if lease is None:
            return False
        # The queue's own class decides first, before the payload is classified: a conditional row is a guarded,
        # recorded text send whatever its payload says, so it never takes another kind's unguarded path, and a
        # finished or unclear record settles it before any check.
        if not lease.conditional and _is_reaction(lease.payload):
            await self._process_reaction(lease)
            return True
        calls = _ProviderCalls(
            self.store, lease, recorded=_record_outbox_calls(lease.payload, is_owner_bot=self.is_owner_bot)
        )
        if calls.recorded and self._settled_by_record(lease):
            return True
        # A send row's writes name this attempt (calls.fence), so after another claim of the row they change nothing.
        try:
            # The stored condition verifies the already pinned route. It never chooses a bot or permits a plain send.
            condition = _owner_dm_condition(lease.payload) if lease.conditional else None
            if condition is None:
                client = await self._ready_client()
            else:
                if self.is_owner_bot or lease.payload.get("account_id") != condition["account_id"]:
                    raise PlaceHeld(OWNER_DM_WAITS)
                client = await self._owner_dm_client()
            if client is None:
                raise self._unready()
            account = self._ensure_pinned_account(lease.payload)
            thread_id = str(lease.payload.get("topic_id") or "")
            owner_binding = self._owner_place_binding(lease.payload, thread_id=thread_id)
            send_client = self._bound_owner_client(client, owner_binding) if owner_binding is not None else client
            reporting = await self._resolved_reporting(lease.payload, account if condition is not None else "")
            lease = replace(lease, payload={**lease.payload, "_reporting": reporting})
            calls.checkpoint(lease.payload)

            async def before_send():
                # The row's own forum topic, as on the reaction path: a topic's place may hold what its chat allows.
                await self._check_place(lease.payload, thread_id=thread_id,
                                        account_id=account, place_binding=owner_binding)
                if outbox_files.records(lease.payload):
                    # Only the bytes queued are ever sent: a kept copy that changed fails before the call. Checked
                    # after the place, so a held row reads nothing.
                    await asyncio.to_thread(outbox_files.verify, lease.payload)

            receipt = await _deliver(
                send_client, lease, self.store, before_send=before_send, account_id=account,
                delivered=self._note_delivered, calls=calls,
                check_owner_dm=(lambda: self._check_owner_dm(send_client, condition)) if condition is not None else None,
            )
            self.store.mark_delivered(lease.delivery_id, provider_receipt=receipt, attempts=calls.fence)
            return True
        except PlaceHeld as exc:
            self.store.hold_outbox(lease.delivery_id, str(exc), attempts=calls.fence, kind=_hold_kind(exc))
            return True
        except RetiredTarget as exc:
            self._end_unsent(lease.delivery_id, str(exc), attempts=calls.fence)
            return True
        except asyncio.CancelledError:
            # A send call already stored as started keeps its marker, so the next claim ends the row uncertain. An
            # attempt cancelled after another claim took its row leaves that claim's lease as it is.
            self.store.release_outbox(
                lease.delivery_id, reason="worker_cancelled", retry_after_sec=0, attempts=calls.fence
            )
            raise
        except LeaseLost as exc:
            # Another claim holds the row now, with its own lease and record: this attempt changes nothing in it.
            self._last_error = _error(exc)
            if exc.message_ids:
                # Telegram's yes to this attempt's call came after that claim: kept only in this log line.
                self._log("warning", f"telegram-bot: {lease.delivery_id} attempt {lease.attempts}: Telegram "
                                     f"confirmed message_id {exc.message_ids} in chat {lease.payload.get('chat_id')} "
                                     "after another claim took the row; not recorded there")
            return True
        except RelayRefusalError as exc:
            self._last_error = _error(exc)
            if exc.relay_refusal in _RELAY_HOLD_REFUSALS:
                self.store.hold_outbox(
                    lease.delivery_id,
                    f"Held: {exc.description}",
                    attempts=calls.fence,
                    kind=_relay_hold_kind(exc.relay_refusal),
                    retry_after_sec=_relay_hold_retry_after(exc),
                )
                return True
            if exc.relay_refusal in _RELAY_TERMINAL_REFUSALS or lease.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self._end_unsent(lease.delivery_id, _error(exc), attempts=calls.fence)
                return True
            self.store.release_outbox(
                lease.delivery_id,
                reason=_error(exc),
                retry_after_sec=max(_retry_delay(lease.attempts), float(exc.retry_after or 0.0)),
                attempts=calls.fence,
            )
            return True
        except Exception as exc:
            if isinstance(exc, AlbumIncomplete) or calls.open is not None or (
                    lease.payload.get("kind") == "operation" and isinstance(exc, TelegramApiError)
                    and not isinstance(exc, TelegramRequestNotSent) and exc.error_code == 0):
                # Telegram may have performed the open send call or this mutation before the answer was lost, and
                # it did accept this album. Do not make a second physical call merely because this lease can be
                # reclaimed.
                self._end_uncertain(lease.delivery_id, _error(exc), attempts=calls.fence)
                return True
            if calls.recorded and self._settled_by_stored_record(lease, exc):
                return True
            if lease.attempts >= _MAX_OUTBOX_ATTEMPTS:
                payload = self.store.outbox_payload(lease.delivery_id)
                # A recorded-call row gets here only through failures before a call and answers that Telegram did
                # nothing; standalone B edits and moderation keep their earlier rule.
                state = ("failed" if lease.conditional or _record_outbox_calls(payload, is_owner_bot=self.is_owner_bot)
                         or isinstance(exc, TelegramRequestNotSent)
                         or (isinstance(exc, TelegramApiError) and exc.error_code)
                         else "uncertain")
                report = (None if payload.get("kind") == "moderation"
                          else delivery_report(lease.delivery_id, payload, part_id="status", state=state, error=_error(exc)))
                self.store.mark_outbox_failed(lease.delivery_id, reason=_error(exc), report=report,
                                              attempts=calls.fence)
            else:
                wait = getattr(exc, "retry_after", None) or 0.0
                self.store.release_outbox(
                    lease.delivery_id,
                    reason=_error(exc),
                    retry_after_sec=max(_retry_delay(lease.attempts), float(wait)),
                    attempts=calls.fence,
                )
            self._last_error = _error(exc)
            return True

    def _settled_by_stored_record(self, lease: OutboxLease, exc: Exception) -> bool:
        """After a send attempt failed with no call open, before its retry and attempt limit: the row as stored now may
        already be decided, as when Telegram confirmed every part, the record keeps each and only marking the row
        delivered failed. Then it ends from that record now (``_settled_by_record``, this attempt's identity), or, if
        the store cannot take that, at the claim after this lease: never failed for a confirmed send, never sent again.
        When the store cannot even be read, nothing more is written now and the claim after this lease decides.
        ``False`` leaves the ordinary retry and limit."""
        try:
            stored = self.store.outbox_payload(lease.delivery_id)
        except Exception as read_error:
            self._last_error = _error(read_error)
            return True
        if not self._settled_by_record(replace(lease, payload=stored, unrecorded_attempt=False)):
            return False
        self._last_error = _error(exc)
        return True

    def _settled_by_record(self, lease: OutboxLease) -> bool:
        """End a send row that its own record already decides, before any token, getMe, Host or place read, so the
        conversation's next row is not held behind it; ``False`` leaves the row to the call it still has to make.

        Every part's accepted message is stored: ``delivered`` from those receipts, with the reporting and account
        the row was recorded with. A stored answer names no message, or an album's does not name one per item:
        ``uncertain``. A call stored as started without its full answer after it, whatever ended that attempt (a
        crash, a restart, a cancel or a stale lease), or an earlier version's attempt that left no proof that it did
        nothing and no full answer: it may have reached Telegram, so ``uncertain``, and nothing is sent.
        """
        payload = lease.payload
        end, value = _recorded_end(payload)
        # An open call outweighs a record that looks complete: an album answer with an extra entry without a
        # message_id keeps the call open beside the messages it named.
        if (CALL_STARTED in payload and end != "uncertain") or (not end and lease.unrecorded_attempt):
            marker = payload.get(CALL_STARTED)
            method = str(marker.get("method") or "") if isinstance(marker, dict) else _send_method(payload)
            end, value = "uncertain", _error(TelegramApiError(method or "send", "send_result_unrecorded"))
        if not end:
            return False
        try:
            if end == "delivered":
                self.store.mark_delivered(lease.delivery_id, provider_receipt=value, attempts=lease.attempts)
            else:
                self._end_uncertain(lease.delivery_id, value, attempts=lease.attempts)
        except Exception as exc:
            # The row stays leased as it is; the claim after its lease decides the same way.
            self._last_error = _error(exc)
        return True

    def _end_unsent(self, delivery_id: str, error: str, *, attempts: Optional[int] = None) -> None:
        """End a row whose current part was not sent and never will be (a refusal before any provider call, or its bot
        or connection ended): failed, with what earlier parts Telegram confirmed kept in the record."""
        payload = self.store.outbox_payload(delivery_id)
        report = (None if payload.get("kind") == "moderation"
                  else delivery_report(delivery_id, payload, part_id="status", state="failed", error=error))
        self.store.mark_outbox_failed(delivery_id, reason=error, report=report, attempts=attempts)
        self._last_error = error

    def _end_uncertain(self, delivery_id: str, error: str, *, attempts: Optional[int] = None) -> None:
        """End a row whose call may have reached Telegram: confirmed parts and kept file copies stay, nothing is
        sent again. ``attempts`` keeps a recorded attempt's end to the row it still holds."""
        payload = self.store.outbox_payload(delivery_id)
        report = (None if payload.get("kind") == "moderation"
                  else delivery_report(delivery_id, payload, part_id="status", state="uncertain", error=error))
        self.store.mark_outbox_failed(delivery_id, reason=error, report=report, state="uncertain", attempts=attempts)
        self._last_error = error

    def _note_delivered(self, chat_id: str, topic_id: Any) -> None:
        """A message of this bot reached that conversation: a "typing…" asked for before it has ended."""
        self._delivered_at[(str(chat_id), str(topic_id or ""))] = time.time()

    async def _typing_worker(self) -> None:
        while not self._stopping:
            try:
                await self.process_typing()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("warning", f"telegram-bot: typing indicator skipped: {_error(exc)}")
            await self._pause(1.0)

    async def process_typing(self) -> int:
        """Show "typing…" once per conversation that a live turn asked for; returns how many were sent.

        Not speech and not custody: no outbox row, receipt or delivery report. A request ends at the first message
        delivered to its conversation after it, when its turn is no longer live, after ``_TYPING_CAP_SECONDS``, when
        the place may not act there now (read before every showing), or at any Telegram refusal, including 429,
        which is never retried. The first three are checked again after the awaited client and place reads, just
        before the call. Several turns asking for one conversation share one renewal. An idle check is a plain read.
        """
        requests = self.store.typing_requests()
        if not requests:
            self._typing_sent_at.clear()
            return 0

        active_account = canonical_bot_id(self._bot_id) if self.is_owner_bot else ""

        def belongs_to_bot(row: Dict[str, Any], account: str) -> bool:
            # Both current and historical requests identify their source as telegram:<bot>:<update>.
            return bool(account and str(row.get("source_event_id") or "").startswith(f"telegram:{account}:"))

        def row_binding(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if not self.is_owner_bot:
                return None
            raw = row.get("place_binding_json")
            if not raw or not isinstance(raw, str):
                return None
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                return None
            return normalize_place_binding(
                parsed,
                account_id=active_account,
                conversation_id=str(row["chat_id"]),
                thread_id=str(row["topic_id"] or ""),
            )

        def over(row: Dict[str, Any], key: Tuple[str, str], now: float) -> bool:
            if self.is_owner_bot and (not belongs_to_bot(row, active_account) or row_binding(row) is None):
                return True
            return (now - float(row["requested_at"]) > _TYPING_CAP_SECONDS
                    or self._delivered_at.get(key, 0.0) >= float(row["requested_at"])
                    or not self.store.turn_live(row["source_event_id"], row["turn_ref"]))

        now = time.time()
        ended: List[Dict[str, Any]] = []
        live: Dict[Tuple[str, str, str, int], Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]] = {}
        for row in requests:
            conv_key = (str(row["chat_id"]), str(row["topic_id"] or ""))
            if over(row, conv_key, now):
                ended.append(row)
            else:
                binding = row_binding(row)
                group_key = (
                    conv_key[0],
                    conv_key[1],
                    binding["place_id"] if binding is not None else "",
                    binding["connected_revision"] if binding is not None else -1,
                )
                if group_key not in live:
                    live[group_key] = (binding, [row])
                else:
                    live[group_key][1].append(row)
        sent = 0
        for group_key, (owner_binding, rows) in live.items():
            key = (group_key[0], group_key[1])
            last = self._typing_sent_at.get(key)
            if last is not None and now - last < _TYPING_RENEW_SECONDS:
                continue
            try:
                client = await self._ready_client()
                if client is None:
                    raise self._unready()
                # Standalone identity may have been confirmed for the first time, or changed, in _ready_client.
                account = canonical_bot_id(self._bot_id)
                unmatched = [row for row in rows if not belongs_to_bot(row, account)]
                ended.extend(unmatched)
                rows = [row for row in rows if row not in unmatched]
                if not rows:
                    continue
                send_client = self._bound_owner_client(client, owner_binding) if owner_binding is not None else client
                # Every showing: Silent, Paused or another current place policy stops it.
                await self._check_place(
                    {"chat_id": key[0]},
                    thread_id=key[1],
                    account_id=account,
                    place_binding=owner_binding,
                )
                # The client/identity can change while the place is read. Do not show with the old or new bot.
                if (self._client is not client or canonical_bot_id(self._bot_id) != account
                        or (not self.is_owner_bot and str(self.token_provider() or "").strip() != self._client_token)):
                    ended.extend(rows)
                    continue
                # A reply delivered, the turn's end or the cap met during those awaits ends a request unsent.
                at = time.time()
                stale = [row for row in rows if over(row, key, at)]
                if stale:
                    ended.extend(stale)
                    rows = [row for row in rows if row not in stale]
                    if not rows:
                        continue
                # Only a forum topic is a thread; a turn's topic_id is never a reply chain (events.py).
                await send_client.send_chat_action(key[0], "typing", topic_id=int(key[1]) if key[1] else None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("info", f"telegram-bot: typing stopped for chat {key[0]}: {_error(exc)}")
                ended.extend(rows)
                continue
            self._typing_sent_at[key] = at
            sent += 1
        if ended:
            self.store.forget_typing(ended)
            live_conv_keys = {(gk[0], gk[1]) for gk in live}
            for key in {(str(row["chat_id"]), str(row["topic_id"] or "")) for row in ended} - live_conv_keys:
                self._typing_sent_at.pop(key, None)
        return sent

    async def _resolved_reporting(self, payload: Dict[str, Any], account_id: str = "") -> Dict[str, Any]:
        reporting = dict(payload.get("_reporting") or {"version": 0})
        discover = getattr(self.submitter, "discover_delivery_support", None)
        if reporting.get("version") == -1:
            reporting["version"] = await discover() if discover else 0
            reporting["status"] = getattr(self.submitter, "delivery_reporting_status", "unsupported")
        if reporting.get("version") == 1 and (account_id or not reporting.get("account_id")):
            reporting["account_id"] = account_id or self._pinned_account(payload) or self._bot_id
        if reporting.get("version") == 1:
            if _is_reaction(payload) and discover:
                await discover()
            reporting["explicit_completion"] = getattr(self.submitter, "presence_explicit_completion", 0)
        return reporting

    async def _check_place(
        self,
        payload: Dict[str, Any],
        *,
        thread_id: str = "",
        account_id: str = "",
        place_binding: Optional[Dict[str, Any]] = None,
    ) -> None:
        target_account = account_id or self._bot_id
        chat_id = str(payload.get("chat_id") or "")
        if self.is_owner_bot and place_binding is not None:
            owner_check = getattr(self.submitter, "check_owner_place", None)
            if callable(owner_check):
                reason = await owner_check(
                    place_binding,
                    account_id=target_account,
                    conversation_id=chat_id,
                    thread_id=thread_id,
                )
                if reason and getattr(reason, "kind", "") == HOLD_BINDING_CHANGED:
                    raise RetiredTarget(str(reason))  # that connection ended: it never comes back
                if reason:
                    raise PlaceHeld(reason)
                return
        check = getattr(self.submitter, "place_hold", None)
        if check:
            reason = await check(target_account, chat_id,
                                 **({"thread_id": thread_id} if thread_id else {}))
            if reason:
                raise PlaceHeld(reason)

    async def _owner_dm_client(self) -> TelegramClient:
        """Require a confirmed client for the current configuration before the ordinary pinned-bot check."""
        try:
            client = await self._ready_client()
            token = str(self.token_provider() or "").strip()
        except Exception as exc:
            self._last_error = _error(exc)
            raise PlaceHeld(PLACE_POLICY_UNREAD) from None
        if (client is None or client is not self._client or not token or token != self._client_token
                or not canonical_bot_id(self._bot_id)):
            raise PlaceHeld(PLACE_POLICY_UNREAD)
        return client

    async def _check_owner_dm(self, client: TelegramClient, condition: Dict[str, Any]) -> None:
        """Right before each physical POST of a conditional row: this very client is still the configured one and is
        the bot the condition names (known from its own getMe, so no new read), Telegram's getChat names the
        condition's private chat, and Ouroboros confirms the whole condition. A typed confirmation change retires the
        unsent remainder. Other nonconfirmations hold before the POST and never permit a plain send."""
        def same_bot() -> bool:
            return (client is self._client and self._bot_id == condition["account_id"]
                    and str(self.token_provider() or "").strip() == self._client_token)

        confirm = getattr(self.submitter, "owner_dm_hold", None)
        if confirm is None or not same_bot():
            raise PlaceHeld(OWNER_DM_WAITS)
        try:
            chat = await client.get_chat(condition["conversation_id"])
        except Exception as exc:
            self._last_error = _error(exc)
            raise PlaceHeld(PLACE_POLICY_UNREAD) from None
        if not (_telegram_id(chat) == condition["conversation_id"] and chat.get("type") == "private"):
            raise PlaceHeld(OWNER_DM_WAITS)
        try:
            reason = await confirm(condition)
        except Exception:
            raise PlaceHeld(PLACE_POLICY_UNREAD) from None  # its text may quote the condition
        if isinstance(reason, HoldNote) and reason.kind == HOLD_BINDING_CHANGED:
            raise RetiredTarget(str(reason))
        if reason != "":
            raise PlaceHeld(reason if isinstance(reason, str) and reason else OWNER_DM_WAITS)
        # A change during these reads invalidates this client; the next claim checks the newly configured bot.
        if not same_bot():
            raise PlaceHeld(OWNER_DM_WAITS)

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
                raise self._unready()
            account = self._ensure_pinned_account(known)
            thread_id = str(known.get("topic_id") or "")
            owner_binding = self._owner_place_binding(known, thread_id=thread_id)
            send_client = self._bound_owner_client(client, owner_binding) if owner_binding is not None else client
            known["_reporting"] = await self._resolved_reporting(known)
            self.store.checkpoint_outbox(lease.delivery_id, known)
            parameters = known.get("parameters")
            if not str(known.get("chat_id") or "").strip() or not isinstance(parameters, dict):
                raise ValueError("Telegram reaction chat_id and parameters are required")
            # The topic the tool's guard checked, so a topic paused after queueing holds this row too.
            await self._check_place(known, thread_id=thread_id, account_id=account, place_binding=owner_binding)
            self.store.checkpoint_outbox(lease.delivery_id, {**known, _REACTION_MARKER: True})
            known[_REACTION_MARKER] = True
            result = await send_client.operation(REACTION_METHOD, dict(parameters))
            if result.get("ok") is False:
                raise TelegramApiError(REACTION_METHOD, "Telegram did not confirm the reaction")
            known["_operation_result"] = result
        except PlaceHeld as exc:
            self.store.hold_outbox(lease.delivery_id, str(exc), kind=_hold_kind(exc))
        except RetiredTarget as exc:
            # Raised before its marker: nothing was asked of Telegram.
            self._settle_reaction(lease, known, state="failed", error=str(exc))
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
        if isinstance(exc, TelegramRequestNotSent):
            if known.get(_REACTION_MARKER):
                del known[_REACTION_MARKER]
                self.store.checkpoint_outbox(lease.delivery_id, known)
            if isinstance(exc, RelayRefusalError):
                if exc.relay_refusal in _RELAY_HOLD_REFUSALS:
                    self.store.hold_outbox(
                        lease.delivery_id,
                        f"Held: {exc.description}",
                        kind=_relay_hold_kind(exc.relay_refusal),
                        retry_after_sec=_relay_hold_retry_after(exc),
                    )
                    return
                if exc.relay_refusal in _RELAY_TERMINAL_REFUSALS:
                    self._settle_reaction(lease, known, state="failed", error=_error(exc))
                    return
            if lease.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self._settle_reaction(lease, known, state="failed", error=_error(exc))
            else:
                wait = max(_retry_delay(lease.attempts), float(getattr(exc, "retry_after", None) or 0.0))
                self.store.release_outbox(lease.delivery_id, reason=_error(exc), retry_after_sec=wait)
            return
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


def _owner_dm_condition(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A conditional row's stored condition, only while it still names this very text message; else the row waits."""
    condition = payload.get(OWNER_DM)
    if not (isinstance(condition, dict) and str(payload.get("kind") or "message") == "message"
            and not payload.get("topic_id")
            and (condition.get("provider"), condition.get("conversation_id"), condition.get("thread_id"))
            == ("telegram", payload.get("chat_id"), "")
            and isinstance(condition.get("account_id"), str) and _BOT_ID.fullmatch(condition["account_id"])):
        raise PlaceHeld(OWNER_DM_WAITS)
    return condition


def _telegram_id(value: Any) -> str:
    """A Telegram Chat's positive integer ``id`` as text; empty for anything else."""
    ident = value.get("id") if isinstance(value, dict) else None
    return str(ident) if type(ident) is int and ident > 0 else ""


def _owner_dm_post(before_send: Optional[Callable[[], Awaitable[None]]], check: Callable[[], Awaitable[None]],
                   calls: "_ProviderCalls", record: Dict[str, Any], part: int):
    """send_text_chunk's ``perform_post`` for a conditional row. Before each physical POST, the plain retry that the
    strict API permits included, ``before_send`` (the row's own place check, which the chunk loop already ran for the
    first POST) and ``check`` run, and only then is the call stored as started, so a held row carries no marker.
    Telegram's own 400 to a first POST has cleared its marker (_not_performed), so a hold before the retry leaves
    nothing open and nothing sent twice."""
    first = True

    async def post(request: Callable[[], Awaitable[Any]]) -> Any:
        nonlocal first
        if not first and before_send:
            await before_send()
        first = False
        await check()
        return await calls.make(record, part, "sendMessage", request)

    return post


def _not_performed(exc: BaseException) -> bool:
    """Telegram's own answer that the call did nothing, or a request proved never to have left this machine."""
    return isinstance(exc, TelegramRequestNotSent) or (
        isinstance(exc, TelegramApiError) and exc.error_code in _NOT_PERFORMED)


class _ProviderCalls:
    """The provider calls of one send attempt (a text chunk, a file or an album), each stored as started first.

    ``open`` names the call stored as started whose accepted message is not stored yet. While it is set, any failure,
    a lost answer, an answer that names no message or a store's failure after Telegram's answer included, leaves the
    call's outcome unknown: the row ends uncertain and that call is never made again. Telegram's answer that it did
    nothing (or a request proved never to have left) removes the marker, so a retry may make the call. An earlier
    version's unrecorded attempt never gets here (TelegramTransportRuntime._settled_by_record). Edits and moderation
    make their calls unrecorded, as before.

    ``fence`` is the attempt's identity, its claim's attempts count, for a send row (``None`` for edits and
    moderation, whose writes stay as before). Every write of a send row names it, as start_outbox_call always did, so
    once another claim holds the row this attempt stores nothing there and ends with ``LeaseLost``.
    """

    def __init__(self, store: CustodyStore, lease: OutboxLease, *, recorded: Optional[bool] = None):
        self.store, self.lease = store, lease
        # A conditional row always needs its attempt's fence, even when the caller passes recorded explicitly.
        self.recorded = lease.conditional or (recorded_calls(lease.payload) if recorded is None else bool(recorded))
        self.fence: Optional[int] = lease.attempts if self.recorded else None
        self.open: Optional[Dict[str, Any]] = None

    def upload(self) -> Optional["_Upload"]:
        """The observer of the file request this attempt is about to make, stored in its row (send rows only)."""
        return _Upload(self.store, self.lease.delivery_id, self.fence) if self.fence is not None else None

    def checkpoint(self, payload: Dict[str, Any], *, report=None, sent=None, confirmed: Sequence[int] = ()) -> None:
        """Store the row's record (with its reports and own messages) as this attempt has it; ``confirmed`` names the
        messages Telegram just confirmed, carried by ``LeaseLost`` if the record cannot be stored."""
        stored = self.store.checkpoint_outbox(self.lease.delivery_id, payload, report=report, sent=sent,
                                              attempts=self.fence)
        if not stored and self.fence is not None:
            raise LeaseLost("this attempt no longer holds the outbox row; its record was not stored", confirmed)

    async def make(self, payload: Dict[str, Any], part: int, method: str, call: Callable[[], Awaitable[Any]],
                   *, upload: Optional["_Upload"] = None) -> Any:
        """Make one call; ``payload`` is the row's record as it stands just before it. ``upload`` observes its file
        request; however this call ends, a cancel included, a request still running in its thread stops writing."""
        if not self.recorded:
            return await call()
        marker = {"part": part, "method": method}
        if not self.store.start_outbox_call(self.lease.delivery_id, self.lease.attempts,
                                            {**payload, CALL_STARTED: marker}):
            raise LeaseLost("this attempt no longer holds the outbox row; nothing was sent")
        self.open = marker
        try:
            return await call()
        except Exception as exc:
            if _not_performed(exc):
                self.checkpoint(payload)  # Telegram did nothing: the marker goes, while this attempt holds the row
                self.open = None
            raise
        finally:
            if upload is not None:
                upload.stop()

    def answered(self) -> None:
        """The open call's answer is stored, in the commit that also removed its marker."""
        self.open = None


class _Upload:
    """One send attempt's file requests as this process sees them (api.UploadProgress), kept in the row's
    ``transfer_json`` for ``telegram_receipt``; called from the transport's thread, ``stop`` from the event loop.

    Each store names the attempt (CustodyStore.record_transfer), so it also keeps the lease of an upload that moves.
    Stopping this attempt is local and checked before the next write; another claimant is discovered only when the
    next progress store reports that the row is no longer held, so more pieces may be written until then. After
    the closing boundary the answer is still read, as nothing is withheld then. The newest observation stays when
    the row ends. Its saved byte count is the last lower bound of completed local writes: an interrupted socket
    write may have emitted part of an uncounted piece, and completed writes need not be saved yet. It is never
    proof that Telegram has them.
    """

    def __init__(self, store: CustodyStore, delivery_id: str, attempt: int):
        self.store, self.delivery_id, self.attempt = store, delivery_id, attempt
        self.stopped = threading.Event()
        self.request = 0
        self.record: Dict[str, Any] = {}
        self._store_attempted_at = 0.0

    def stop(self) -> None:
        self.stopped.set()

    def begin(self, file_bytes_total: int) -> None:
        self.check_active()
        self.request += 1  # 2 is the plain-caption request after Telegram refused the first's HTML
        now = time.time()
        self.record = {"attempt": self.attempt, "request": self.request, "phase": "connecting",
                       "file_bytes_written": 0, "file_bytes_total": int(file_bytes_total),
                       "started_at": now, "updated_at": now}
        self._store(abandon=True)

    def wrote(self, file_bytes_written: int) -> None:
        self.check_active()
        new_phase = self.record["phase"] != "uploading"
        if not new_phase and file_bytes_written == self.record["file_bytes_written"]:
            return
        self.record.update(phase="uploading", file_bytes_written=int(file_bytes_written), updated_at=time.time())
        if new_phase or monotonic() - self._store_attempted_at >= _TRANSFER_RECORD_SECONDS:
            self._store(abandon=True)

    def sent(self) -> None:
        if self.stopped.is_set():
            return  # a stopped attempt's row is no longer its own to write
        self.record.update(phase="awaiting_response", file_bytes_written=self.record["file_bytes_total"],
                           updated_at=time.time())
        self._store(abandon=False)

    def ended(self, answer: str, error_code: int = 0) -> None:
        if self.stopped.is_set():
            return
        now = time.time()
        self.record.update(ended_at=now, answer=answer, **({"error_code": int(error_code)} if answer == "refused" else {}))
        self._store(abandon=False)

    def check_active(self) -> None:
        if self.stopped.is_set():
            raise LeaseLost("this attempt was stopped; its file request is abandoned before its next write")

    def _store(self, *, abandon: bool) -> None:
        try:
            held = self.store.record_transfer(self.delivery_id, self.attempt, self.record,
                                              lease_sec=_UPLOAD_LEASE_SECONDS)
        except sqlite3.Error:
            held = None  # An unavailable observation is not evidence of a lost lease.
        finally:
            self._store_attempted_at = monotonic()  # even a failed write is throttled
        if abandon:
            self.check_active()  # the async worker may have stopped while this write waited for SQLite
        if held is False and abandon:
            raise LeaseLost("this attempt no longer holds the outbox row; its file request is abandoned")


def _send_method(payload: Dict[str, Any]) -> str:
    kind = str(payload.get("kind") or "message")
    if kind == "moderation":
        action = str(payload.get("action") or "")
        return _MODERATION_METHODS.get(action, action)
    if kind == "operation":
        return str(payload.get("method") or "")
    return "sendMessage" if kind == "message" else "sendMediaGroup" if kind == "album" else MEDIA_METHODS.get(kind, "")


def _album_incomplete(returned: int, named: int, items: int) -> AlbumIncomplete:
    unnamed = f" and {returned - named} entries without a message_id" if returned > named else ""
    return AlbumIncomplete("sendMediaGroup", f"Telegram accepted the album and returned {named} messages{unnamed} for "
                           f"{items} items; completeness is unknown, and it is not sent again")


def _recorded_end(payload: Dict[str, Any]) -> Tuple[str, Any]:
    """What a recorded outbox row's stored answers already decide, with no call.

    ``("delivered", provider receipt)`` when every part's accepted message (or an owner-bot A edit/moderation
    result) is stored; ``("uncertain", error)`` when a stored send answer names no message, or an album's does
    not name one message per item (fewer, more, or entries without a message_id); ``("", None)`` while a call is
    still to be made.
    """
    kind = str(payload.get("kind") or "message")
    if kind == "moderation":
        if "_moderation_result" not in payload:
            return "", None
        return "delivered", {"kind": kind, "action": payload.get("action"), "result": payload["_moderation_result"]}
    if kind == "operation":
        if "_operation_result" not in payload:
            return "", None
        return "delivered", {"kind": kind, "method": str(payload.get("method") or ""), "result": payload["_operation_result"]}
    if kind == "message":
        messages = payload.get("_sent_messages")
        if messages is None:
            return "", None
        if not isinstance(messages, list) or not all(names_message(message) for message in messages):
            return "uncertain", _error(UnconfirmedSend("sendMessage"))
        chunks = payload.get("_rendered_chunks")
        # A row from before formatting keeps the split it was sent with (_deliver).
        count = len(chunks) if isinstance(chunks, list) else len(_split_text(str(payload.get("text") or ""), 4000))
        return ("delivered", {"kind": kind, "messages": messages}) if messages and len(messages) >= count else ("", None)
    if "_sent_media" not in payload:
        return "", None
    stored = payload["_sent_media"]
    if kind == "album":
        items = payload.get("_rendered_items") or payload.get("items") or []
        returned = stored if isinstance(stored, list) else []
        named = [message for message in returned if names_message(message)]
        if len(returned) == len(named) == len(items):
            return "delivered", {"kind": kind, "messages": named}
        return "uncertain", _error(_album_incomplete(len(returned), len(named), len(items)))
    if names_message(stored):
        return "delivered", {"kind": kind, "message": stored}
    return "uncertain", _error(UnconfirmedSend(_send_method(payload) or "send"))


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
    delivered: Callable[[str, Any], None] = lambda _chat, _topic: None, calls: Optional[_ProviderCalls] = None,
    check_owner_dm: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    calls = calls or _ProviderCalls(store, lease)
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
            calls.checkpoint(payload)
        for index in range(len(messages), len(chunks)):
            chunk = chunks[index]
            if before_send:
                await before_send()
            # The record with every chunk confirmed so far: the marker is stored with it, never in place of it.
            current = {**payload, "_sent_messages": messages} if messages or "_sent_messages" in payload else payload
            if check_owner_dm is None:
                result = await calls.make(current, index, "sendMessage", lambda: client.send_text_chunk(
                    chat_id,
                    chunk["text"],
                    parse_mode=chunk["parse_mode"],
                    topic_id=topic_id,
                    reply_to_message_id=reply_id if index == 0 else None,
                ))
            else:
                # perform_post runs before_send (the first POST had it above) and the check before each physical POST,
                # the plain retry included, and only then stores the started marker: a held row carries none, and a
                # first POST with an unknown answer sends no second one.
                result = await client.send_text_chunk(
                    chat_id, chunk["text"], parse_mode=chunk["parse_mode"], topic_id=topic_id,
                    reply_to_message_id=reply_id if index == 0 else None,
                    perform_post=_owner_dm_post(before_send, check_owner_dm, calls, current, index))
            if not names_message(result):
                raise UnconfirmedSend("sendMessage")  # its marker stays: the row ends uncertain
            report = delivery_report(lease.delivery_id, payload, part_id=index, state="delivered", receipt=result,
                                     text=chunk["text"], fmt="html" if chunk["parse_mode"] == "HTML" else "plain")
            messages.append({key: value for key, value in result.items() if key != "_delivery"})
            calls.checkpoint(
                {**payload, "_sent_messages": messages}, report=report,
                sent=_own_message(account_id, chat_id, topic_id, result, chunk["text"]),
                confirmed=[result["message_id"]],
            )
            calls.answered()
            delivered(chat_id, topic_id)
        return {"kind": kind, "messages": messages}
    if kind == "moderation":
        if before_send:
            await before_send()
        action = str(payload["action"])
        if calls.recorded:
            result = await calls.make(
                payload,
                0,
                _MODERATION_METHODS.get(action, action),
                lambda: client.moderate(action, dict(payload["parameters"])),
            )
            # Moderation has no speech delivery report; checkpoint clears CALL_STARTED with the stored result.
            calls.checkpoint({**payload, "_moderation_result": result})
            calls.answered()
            return {"kind": kind, "action": payload["action"], "result": result}
        result = await client.moderate(action, dict(payload["parameters"]))
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
        if calls.recorded:
            result = await calls.make(
                payload, 0, method, lambda: client.operation(method, dict(parameters))
            )
            report = delivery_report(
                lease.delivery_id, payload, part_id="0", state="delivered",
                receipt=result, text=str(payload.get("text") or ""), fmt="plain",
            )
            calls.checkpoint(
                {**payload, "_operation_result": result},
                report=report,
                confirmed=[result["message_id"]] if names_message(result) else (),
            )
            calls.answered()
            return {"kind": kind, "method": method, "result": result}
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
    if kind == "album":
        return await _deliver_album(client, lease, store, before_send=before_send, account_id=account_id,
                                    delivered=delivered, calls=calls)
    if kind not in MEDIA_METHODS:
        raise ValueError(f"unsupported Telegram outbox kind: {kind}")
    # A stored answer (_sent_media) was decided before any call (TelegramTransportRuntime._settled_by_record).
    caption = payload.get("_rendered_caption")
    if caption is None:
        caption = prepare_caption(
            str(payload.get("caption") or ""), markdown=payload.get("markdown", False)
        )
        payload = {**payload, "_rendered_caption": caption}
        calls.checkpoint(payload)
    if before_send:
        await before_send()
    # The row's file: a copy kept when it was queued, or, for a row queued before 0.12.1, the path it named.
    upload = calls.upload()
    result = await calls.make(payload, 0, MEDIA_METHODS[kind], lambda: client.send_media(
        kind,
        chat_id,
        pathlib.Path(str(payload.get("file_path") or "")),
        prepared_caption=caption,
        topic_id=topic_id,
        reply_to_message_id=reply_id,
        progress=upload,
    ), upload=upload)
    if not names_message(result):
        raise UnconfirmedSend(MEDIA_METHODS[kind])  # its marker stays: the row ends uncertain
    report = delivery_report(lease.delivery_id, payload, part_id=0, state="delivered", receipt=result,
                             text=caption["text"], fmt="html" if caption["parse_mode"] == "HTML" else "plain")
    result = {key: value for key, value in result.items() if key != "_delivery"}
    calls.checkpoint({**payload, "_sent_media": result}, report=report,
                     sent=_own_message(account_id, chat_id, topic_id, result, caption["text"]),
                     confirmed=[result["message_id"]])
    calls.answered()
    delivered(chat_id, topic_id)
    return {"kind": kind, "message": result}


async def _deliver_album(
    client: TelegramClient, lease: OutboxLease, store: CustodyStore, *, before_send=None, account_id: str = "",
    delivered: Callable[[str, Any], None] = lambda _chat, _topic: None, calls: Optional[_ProviderCalls] = None,
) -> Dict[str, Any]:
    """One sendMediaGroup call; each message Telegram returns gets its own receipt part and delivery report.

    Telegram's ok is kept as ``_sent_media`` before anything else, so an accepted album is never sent again (a stored
    one is decided before any call: TelegramTransportRuntime._settled_by_record). Only entries that name a message
    are kept. Unless the answer names one message per item, the call stays open in the record and the row ends
    uncertain.
    """
    calls = calls or _ProviderCalls(store, lease)
    payload = lease.payload
    chat_id = str(payload.get("chat_id") or "").strip()
    topic_id = _optional_int(payload.get("topic_id"))
    items = payload.get("_rendered_items")
    if items is None:
        items = [{"kind": item["kind"], "file_path": item["file_path"],
                  "caption": prepare_caption(str(item.get("caption") or ""), markdown=payload.get("markdown", False))}
                 for item in payload.get("items") or []]
        payload = {**payload, "_rendered_items": items}
        calls.checkpoint(payload)
    if before_send:
        await before_send()
    upload = calls.upload()
    results = await calls.make(payload, 0, "sendMediaGroup", lambda: client.send_media_group(
        chat_id, items, topic_id=topic_id, reply_to_message_id=_optional_int(payload.get("reply_to_message_id")),
        progress=upload), upload=upload)
    messages, reports, rows = [], [], []
    for index, result in enumerate(results):
        if not names_message(result):
            continue  # confirms no message, so the album is not complete
        # Keep extra provider messages too; there is no input file to attribute to them.
        item = items[index] if index < len(items) else {
            "kind": next((kind for kind in MEDIA_METHODS if kind in result), "unknown"), "file_path": ""}
        wire = result.get("_delivery") or {}
        reports.append(delivery_report(lease.delivery_id, {**payload, "kind": item["kind"], "file_path": item["file_path"]},
                                       part_id=index, state="delivered", receipt=result, text=wire.get("text", ""),
                                       fmt=wire.get("format", "plain")))
        message = {key: value for key, value in result.items() if key != "_delivery"}
        messages.append(message)
        rows.append(_own_message(account_id, chat_id, topic_id, message, wire.get("text", "")))
    complete = len(results) == len(messages) == len(items)
    # Its confirmed messages are kept either way. An incomplete answer keeps the call open in the record.
    calls.checkpoint({**payload, "_sent_media": messages, **({} if complete else {CALL_STARTED: calls.open})},
                     report=reports, sent=rows, confirmed=[message["message_id"] for message in messages])
    delivered(chat_id, topic_id)
    if not complete:
        raise _album_incomplete(len(results), len(messages), len(items))
    calls.answered()
    return {"kind": "album", "messages": messages}


def _update_id(update: Dict[str, Any]) -> Optional[int]:
    try:
        return int(update.get("update_id"))
    except (AttributeError, TypeError, ValueError):
        return None


def _nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:160] or "event"


def _declared_size(attachment: Dict[str, Any]) -> int:
    try:
        return int(attachment.get("file_size") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_filename(value: str) -> str:
    name = pathlib.PurePath(value.replace("\\", "/")).name
    clean = re.sub(r"[^A-Za-z0-9_. -]+", "_", name).strip(" .")
    if len(clean) > 160:
        # Byte sniffing uses this suffix for tagless MP3 and generic-brand M4A.
        suffix = pathlib.PurePath(clean).suffix
        clean = clean[:160 - len(suffix)] + suffix if len(suffix) < 160 else clean[:160]
    return clean or "attachment"


def _retry_delay(attempts: int) -> float:
    return min(300.0, 2.0 ** min(max(1, attempts), 8))


def _host_record(submitter: Any) -> Dict[str, Any]:
    """The Host's capabilities as one completed /identity check published them (PresenceHostClient.capabilities, read
    once); a submitter without that record (a test's) is read field by field, as before."""
    record = getattr(submitter, "capabilities", None)
    if record is not None and dataclasses.is_dataclass(record):
        return dataclasses.asdict(record)
    return {"status": getattr(submitter, "delivery_reporting_status", ""),  # "": never answered (not checked)
            "continuation_version": getattr(submitter, "continuation_version", 0),
            "reaction_context": getattr(submitter, "presence_reaction_context", 0),
            "explicit_completion": getattr(submitter, "presence_explicit_completion", 0),
            "identity": getattr(submitter, "presence_identity", 0),
            "places_version": getattr(submitter, "places_version", 0),
            "telegram_group_feed": getattr(submitter, "telegram_group_feed", 0),
            "telegram_group_poll_claim": getattr(submitter, "telegram_group_poll_claim", 0),
            "checked_at": getattr(submitter, "identity_checked_at", 0.0),
            "unasked": ""}


def _confirmed(running: bool, token: str, client_token: str, bot_id: str) -> Tuple[str, str]:
    """confirmed_bot()'s answer from one read of the runtime's facts and one read of the token."""
    if not running:
        return "", BOT_NOT_RUNNING
    if not token:
        return "", BOT_TOKEN_MISSING
    if token != client_token or not _BOT_ID.fullmatch(bot_id):
        return "", BOT_UNCONFIRMED
    return bot_id, ""


def _poll_facts(attempt: Dict[str, Any], token: str) -> Dict[str, Any]:
    """The last completed poll attempt, only when it ran with the token/account configured now (local, no network).

    ``poll_result`` is ``ok``, ``failed`` or ``""`` (none completed for this token yet); a failure keeps its own
    error and side (``_poll_failure``), never another loop's newest error.
    """
    if not attempt or not token or attempt.get("token") != token:
        return {"poll_result": "", "poll_at": "", "poll_error": "", "poll_failure": "", "poll_stage": ""}
    return {"poll_result": attempt["result"], "poll_at": _iso(attempt["at"]),
            "poll_error": attempt.get("error", ""), "poll_failure": attempt.get("failure", ""),
            "poll_stage": attempt.get("stage", "")}


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch))) if epoch else ""


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
