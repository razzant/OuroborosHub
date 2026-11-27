"""Model-selected Telegram actions using the existing durable outbox."""

from __future__ import annotations

from dataclasses import dataclass
import pathlib
import re
import sqlite3
from typing import Any, Callable, Optional

from . import outbox_files
from .api import CLOUD_PHOTO_UPLOAD_LIMIT, CLOUD_UPLOAD_LIMIT, MEDIA_METHODS
from .custody import CustodyStore
from .delivery import tool_origin
from .formatting import prepare_caption
from .owner_feed import (
    SOURCE_OWNER_BOT,
    SOURCE_STANDALONE,
    canonical_bot_id,
    normalize_place_binding,
    owner_state_dir,
    owner_store_exists,
    owner_store_path,
)


SEND_KINDS = ("message", *MEDIA_METHODS, "album")
# sendMediaGroup takes these; photos and videos mix, audio and documents group only with their own kind.
ALBUM_KINDS = ("photo", "video", "audio", "document")
# Telegram plays a voice message from OGG/Opus, MP3 or M4A (https://core.telegram.org/bots/api#sendvoice).
_VOICE_SUFFIXES = (".ogg", ".oga", ".opus", ".mp3", ".m4a")
ACCOUNT_ID_SCHEMA = {
    "type": "string",
    "description": (
        "Optional exact numeric Telegram bot account ID. Required when acting through the connected "
        "owner-bot group feed so the Host guard checks that exact place; omit for legacy standalone bot calls."
    ),
}


class SendRefused(ValueError):
    """A typed refusal: nothing was queued, and ``code`` says which Bot API rule the request breaks."""

    def __init__(self, code: str, message: str, **facts: Any):
        super().__init__(message)
        self.code = code
        self.facts = facts

    def answer(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": str(self), **self.facts}


@dataclass(frozen=True)
class ToolSource:
    """Resolved target profile for one tool call (standalone B or owner-bot group feed A)."""

    source: str
    state_dir: pathlib.Path
    store_path: pathlib.Path
    account_id: str
    explicit: bool
    group_only: bool
    turn_bound: bool = False

    @property
    def expose_account(self) -> bool:
        """A legacy call through B keeps its response shape even when its queued row pins the bot."""
        return self.source == SOURCE_OWNER_BOT or self.explicit

    @property
    def pin_account(self) -> bool:
        return self.expose_account or self.turn_bound


def _persisted_owner_account(root: pathlib.Path) -> str:
    if not owner_store_exists(root):
        return ""
    try:
        record = CustodyStore.shared(owner_store_path(root)).confirmed_owner_bot()
    except Exception:
        return ""
    return canonical_bot_id(record.get("account_id") if isinstance(record, dict) else "")


def _turn_event(ctx: Any) -> dict[str, Any]:
    metadata = getattr(ctx, "task_metadata", None) or {}
    event = (metadata.get("presence") or {}).get("event") or {} if isinstance(metadata, dict) else {}
    return event if isinstance(event, dict) and event.get("provider") == "telegram" else {}


def _turn_account_id(ctx: Any) -> str:
    return canonical_bot_id(_turn_event(ctx).get("account_id"))


def _owner_turn(root: pathlib.Path, ctx: Any) -> Optional[bool]:
    """Whether this turn's event came through the owner's own bot: its event is in that bot's own store (every event
    there keeps its place binding). An unreadable store leaves the source unknown."""
    source = str(_turn_event(ctx).get("source_event_id") or "")
    if not source:
        return False
    try:
        if not owner_store_exists(root):
            return False
        return CustodyStore.shared(owner_store_path(root)).inbox_place_binding(source) is not None
    except Exception:
        return None


@dataclass(frozen=True)
class TurnTarget:
    """This turn's own event, when a call acts on it: the store that holds it (``source``), the bot it came through
    and, for the owner's bot, the exact place connection it arrived under (``binding``)."""

    source: str
    account_id: str
    binding: Optional[dict[str, Any]] = None


def _turn_source_unavailable() -> SendRefused:
    return SendRefused(
        "turn_source_unavailable",
        "Could not verify the source of this Telegram turn's action. Nothing was done; try again when its history can be read.",
    )


def turn_target(root: pathlib.Path, ctx: Any, chat_id: Any, *, topic_id: Any = None,
                message_id: Any = None) -> Optional[TurnTarget]:
    """This turn's own event when the call acts in that event's own chat and topic (a send, typing) or on that event's
    own message (``message_id``: a reaction, a deletion); else ``None``. Read from the store that kept the event, so the
    answer keeps the bot and connection the event arrived under, whichever bots are active now."""
    event = _turn_event(ctx)
    source_id, chat = str(event.get("source_event_id") or ""), str(chat_id or "").strip()
    if not source_id or not chat or str(event.get("conversation_id") or "") != chat:
        return None
    target = message_id if message_id is not None else topic_id
    address = str(target).strip() if target not in (None, "") else ""

    def address_of(payload: dict[str, Any]) -> Any:
        if message_id is None:
            return payload.get("thread_id")
        message = payload.get("message")
        return message.get("message_id") if isinstance(message, dict) else None

    for kind, path in ((SOURCE_OWNER_BOT, owner_store_path(root)), (SOURCE_STANDALONE, root / "custody.sqlite3")):
        try:
            if not path.is_file():
                continue
            record = CustodyStore.shared(path).inbox_event(source_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
            # The Host event can rule out this target, but only readable custody can choose its bot/connection.
            declared = address_of(event)
            if declared is not None and str(declared) != address:
                return None
            raise _turn_source_unavailable() from exc
        if record is None:
            continue
        payload, binding = record
        if str(address_of(payload) or "") != address:
            return None
        account = canonical_bot_id(payload.get("account_id"))
        if not account:
            raise _turn_source_unavailable()
        if kind == SOURCE_STANDALONE:
            return TurnTarget(kind, account)
        thread = str(payload.get("thread_id") or "")
        binding = normalize_place_binding(binding, account_id=account, conversation_id=chat, thread_id=thread)
        if binding is None:
            raise _turn_source_unavailable()
        return TurnTarget(kind, account, binding)
    return None


def _not_this_turns_route(account: str) -> SendRefused:
    return SendRefused(
        "account_not_active",
        f"The bot this turn came through ({account}) is not your connected bot for it now. Nothing was queued; this "
        "conversation is not continued through another bot.",
    )


def resolve_tool_source(
    root_state_dir: Any,
    *,
    account_id: Any = "",
    ctx: Any = None,
    standalone_account: Callable[[], str] = lambda: "",
    owner_account: Callable[[], str] = lambda: "",
    standalone_configured: Optional[Callable[[], bool]] = None,
    turn: Optional[TurnTarget] = None,
) -> ToolSource:
    """Resolve explicit A vs standalone B without ever selecting A behind the Host guard.

    - Explicit ``account_id`` must match confirmed A or confirmed B (never falls back to another bot).
    - A call acting on this turn's own event (``turn``, see ``turn_target``) that names the bot the event came
      through keeps that event's route: the owner bot's store for an owner-bot event, the standalone one's for its
      own, whatever role that bot has now; otherwise it is refused.
    - Omitted ``account_id`` never routes to A: if the turn came from A, or A is active without standalone B,
      refuses with ``account_id_required`` so callers pass explicit ``account_id`` at the Host guard.
    - An omitted standalone B account retains the bot of its exact turn target, if known; other legacy B calls
      remain unpinned.
    """
    root = pathlib.Path(root_state_dir)
    raw = str(account_id or "").strip() if account_id is not None else ""
    if raw and not canonical_bot_id(raw):
        raise SendRefused("invalid_account_id", "account_id must be a positive numeric Telegram bot id")
    req = canonical_bot_id(raw) if raw else ""

    bot_b = canonical_bot_id(standalone_account())
    bot_a = canonical_bot_id(owner_account()) or _persisted_owner_account(root)

    def _source_a() -> ToolSource:
        return ToolSource(
            source=SOURCE_OWNER_BOT,
            state_dir=owner_state_dir(root),
            store_path=owner_store_path(root),
            account_id=bot_a,
            explicit=True,
            group_only=True,
        )

    def _source_b(resolved_id: str, *, explicit: bool, turn_bound: bool = False) -> ToolSource:
        return ToolSource(
            source=SOURCE_STANDALONE,
            state_dir=root,
            store_path=root / "custody.sqlite3",
            account_id=resolved_id,
            explicit=explicit,
            group_only=False,
            turn_bound=turn_bound,
        )

    turn_account = _turn_account_id(ctx)
    owner_turn = turn.source == SOURCE_OWNER_BOT if turn is not None else _owner_turn(root, ctx)
    if req:
        if turn is not None:
            # This turn's own event, through the bot it came through: only that event's own route. A bot that now
            # has the other role (or another bot in this one) is never a way to continue it.
            if req != turn.account_id:
                raise SendRefused(
                    "turn_account_mismatch",
                    f"This action belongs to the bot that received the message ({turn.account_id}). Nothing was queued.",
                )
            if owner_turn and req == bot_a:
                return _source_a()
            if not owner_turn and req == bot_b:
                return _source_b(bot_b, explicit=True)
            raise _not_this_turns_route(req)
        if bot_a and req == bot_a and req != bot_b:
            return _source_a()
        if bot_b and req == bot_b:
            return _source_b(bot_b, explicit=True)
        if owner_turn and req == turn_account:
            # The owner's bot this turn came through is not the one connected now: its conversation is not moved to
            # another bot.
            raise _not_this_turns_route(req)
        active = sorted({acc for acc in (bot_a, bot_b) if acc})
        known = ", ".join(active) if active else "none confirmed yet"
        raise SendRefused(
            "unknown_account_id",
            f"account_id {req} does not match an active Telegram bot in this skill (active: {known}).",
        )

    # An omitted account_id is refused only for a turn that came through the owner's bot (its event is in that bot's
    # store, whichever bot is active now: the hint names the turn's own bot, never the current one), or while only the
    # owner's bot is set up. A standalone-B turn and any call while a standalone token is set keep the 0.13.1 path,
    # also before getMe confirmed that bot.
    if owner_turn or (turn is None and turn_account and bot_a and turn_account == bot_a and turn_account != bot_b):
        raise SendRefused(
            "account_id_required",
            f"Pass explicit account_id='{turn_account}' when acting through the owner bot so the Host guard checks that exact place.",
        )
    if turn is not None and turn.source == SOURCE_STANDALONE:
        if (bot_b and bot_b != turn.account_id) or (bot_a == turn.account_id and not bot_b):
            raise _not_this_turns_route(turn.account_id)
        return _source_b(turn.account_id, explicit=False, turn_bound=True)
    b_configured = bool(standalone_configured()) if callable(standalone_configured) else bool(bot_b)
    if bot_a and not b_configured and not bot_b:
        raise SendRefused(
            "account_id_required",
            f"Pass explicit account_id='{bot_a}' when acting through the owner bot so the Host guard checks that exact place.",
        )
    return _source_b(bot_b, explicit=False)


def enforce_source_chat_policy(source: ToolSource, chat_id: str) -> None:
    """Owner-bot group feed tools only target negative numeric group/supergroup chat IDs."""
    if source.group_only and re.fullmatch(r"-[0-9]+", str(chat_id or "").strip()) is None:
        raise SendRefused(
            "owner_bot_group_only",
            "The connected owner bot only acts in numeric Telegram group or supergroup chats (negative chat_id); "
            "private chats stay on the built-in owner bridge.",
        )


def bind_owner_place_or_refuse(
    source: ToolSource,
    chat_id: str,
    *,
    topic_id: Any = "",
    owner_place_binder: Optional[Callable[[str, str, str], dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """Obtain and validate the exact wire-v2 ``owner_bot`` place binding before queueing or copying files.

    Returns ``None`` for standalone B (unchanged). For owner-bot A, refuses with a typed ``SendRefused``
    whenever the exact ``owner_bot`` place is not connected, auth-blocked, or unavailable.
    """
    if source.source != SOURCE_OWNER_BOT:
        return None
    if not callable(owner_place_binder):
        raise SendRefused(
            "place_binding_unavailable",
            "Owner-bot place binder is unavailable. Nothing was queued.",
        )
    thread = str(topic_id).strip() if topic_id not in (None, "") else ""
    if thread and re.fullmatch(r"[1-9][0-9]*", thread) is None:
        raise SendRefused("invalid_topic_id", "topic_id must be a positive numeric forum topic id")
    verdict = owner_place_binder(source.account_id, str(chat_id).strip(), thread)
    if not isinstance(verdict, dict):
        raise SendRefused(
            "place_binding_unavailable",
            "Presence Host did not return a valid owner-bot place binding. Nothing was queued.",
        )
    binding = normalize_place_binding(
        verdict.get("binding"),
        account_id=source.account_id,
        conversation_id=str(chat_id).strip(),
        thread_id=thread,
    )
    state = str(verdict.get("state") or "").strip()
    if binding is not None and (verdict.get("allowed") is True or state in {"active", "silent", "paused"}):
        return binding
    code = str(verdict.get("code") or "").strip() or "place_not_connected"
    error = str(verdict.get("error") or "").strip() or (
        "Connect this group chat for the owner bot in Places first. Nothing was queued."
        if code == "place_not_connected"
        else f"Could not bind owner-bot place ({code}). Nothing was queued."
    )
    raise SendRefused(code, error)


def turn_place_or_bind(
    source: ToolSource,
    turn: Optional[TurnTarget],
    chat_id: str,
    *,
    topic_id: Any = "",
    owner_place_binder: Optional[Callable[[str, str, str], dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """The place connection an owner-bot call acts under. A call on this turn's own event (its chat and topic, or its
    message), through the owner's bot that event came through, keeps the connection the event arrived under: it is never
    bound again to a newer one, and if that connection has ended the row ends unsent. Any other call is bound now."""
    thread = str(topic_id).strip() if topic_id not in (None, "") else ""
    if source.source == SOURCE_OWNER_BOT and turn is not None:
        if (turn.binding is None or turn.binding["conversation_id"] != str(chat_id).strip()
                or turn.binding["thread_id"] != thread):
            raise SendRefused(
                "turn_topic_mismatch",
                "Use the chat and topic of the original Telegram message. Nothing was queued.",
            )
        return turn.binding
    return bind_owner_place_or_refuse(source, chat_id, topic_id=topic_id, owner_place_binder=owner_place_binder)


def _upload_limit(kind: str) -> int:
    return CLOUD_PHOTO_UPLOAD_LIMIT if kind == "photo" else CLOUD_UPLOAD_LIMIT


def upload_path(kind: str, file_path: Any) -> str:
    """An existing file within the cloud Bot API's upload limit for ``kind``; refused, never cut."""
    path = pathlib.Path(str(file_path or "")).expanduser()
    if not path.is_file():
        raise SendRefused("file_missing", "file_path must name an existing file")
    size, limit = path.stat().st_size, _upload_limit(kind)
    if size > limit:
        raise SendRefused("file_too_large", f"{path.name} is {size:,} bytes; the Bot API accepts a {kind} of at most "
                          f"{limit // (1024 * 1024)} MB from a bot. Not queued.", size_bytes=size, limit_bytes=limit)
    if kind == "voice" and not path.name.lower().endswith(_VOICE_SUFFIXES):
        raise SendRefused("voice_format", "Telegram plays a voice message from OGG/Opus, MP3 or M4A; send this file "
                          "as audio or document. Not queued.")
    return str(path.resolve())


def queue_file(kind: str, source: str, state_dir: Any) -> dict[str, Any]:
    """Keep a copy of the bytes of ``source`` now: the row sends that copy, never this path.

    ``source`` is what ``upload_path`` returned for ``kind``: the name the caller gave was checked there, once, and
    this is the file it resolves to. The copy holds the upload limit on the bytes it reads. Returns the row's
    ``_frozen`` record ({path, sha256, size, source}); ``path`` keeps the resolved file's name, which Telegram shows
    for a document, as 0.12.0 sent it. Refused, with nothing kept, when the file cannot be copied.
    """
    limit = _upload_limit(kind)
    try:
        return outbox_files.keep(pathlib.Path(source), state_dir, limit)
    except outbox_files.CopyTooLarge as exc:
        raise SendRefused("file_too_large", f"{exc}; the Bot API accepts a {kind} of at most "
                          f"{limit // (1024 * 1024)} MB from a bot. Not queued.", limit_bytes=limit) from exc
    except OSError as exc:
        raise SendRefused("file_copy_failed", f"{pathlib.Path(source).name} could not be copied for sending "
                          f"({type(exc).__name__}: {exc.strerror or exc}). Not queued.") from exc


def discard_files(records: list[dict[str, Any]], state_dir: Any) -> None:
    """Remove the copies this call kept when it queued nothing (refused, or its request_id was already queued)."""
    outbox_files.discard(records, state_dir)


def discard_unqueued(records: list[dict[str, Any]], state_dir: Any, store: Any, delivery_id: str) -> None:
    """After an enqueue that raised, remove this call's copies only if the row cannot name them.

    SQLite may have committed the row before the error reached this call, and then the copy is that row's only bytes.
    So they are removed only when no store was opened, or the store, read back, has no row ``delivery_id`` naming
    them; they stay when the row names them or the store cannot be read to say.
    """
    if not records:
        return
    if store is not None:
        try:
            named = {str(record["path"]) for record in outbox_files.records(store.outbox_payload(delivery_id))}
        except Exception:
            return  # whether the row was queued is unknown: its only copy stays
        if any(str(record.get("path")) in named for record in records):
            return
    outbox_files.discard(records, state_dir)


def frozen_caption(caption: str, markdown: bool) -> dict[str, str]:
    try:
        return prepare_caption(caption, markdown=markdown)
    except ValueError as exc:
        raise SendRefused("caption_too_long", str(exc)) from exc


def album_items(items: Any, caption: str, markdown: bool,
                state_dir: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The album as queued, as it will be sent, after Telegram's composition rules (sendMediaGroup), and the copies.

    With ``state_dir`` each file is copied once every check has passed, and both lists name the copies; without it
    the files are only checked and no copy is kept.
    """
    if not isinstance(items, list) or not 2 <= len(items) <= 10:
        raise SendRefused("album_size", "an album has 2 to 10 items. Not queued.")
    if any(not isinstance(item, dict) for item in items):
        raise SendRefused("album_item", "each album item is {kind, file_path, caption}. Not queued.")
    kinds = [str(item.get("kind") or "") for item in items]
    other = next((kind for kind in kinds if kind not in ALBUM_KINDS), None)
    if other is not None:
        raise SendRefused("album_kind", f"an album holds photo, video, audio or document items; send {other or 'that'} "
                          "on its own. Not queued.")
    if not (set(kinds) <= {"photo", "video"} or len(set(kinds)) == 1):
        raise SendRefused("album_mixed", "Telegram groups photos with videos, audio only with audio and documents only "
                          "with documents. Not queued.")
    captions = [str(item.get("caption") or "") for item in items]
    if caption and any(captions):
        raise SendRefused("album_caption", "give the album one caption, or captions on its items, not both. Not queued.")
    captions[0] = captions[0] or caption
    paths = [upload_path(kind, item.get("file_path")) for kind, item in zip(kinds, items)]
    rendered = [frozen_caption(text, markdown) for text in captions]
    kept: list[dict[str, Any]] = []
    if state_dir is not None:
        try:
            for kind, path in zip(kinds, paths):
                kept.append(queue_file(kind, path, state_dir))
        except BaseException:
            discard_files(kept, state_dir)
            raise
        paths = [record["path"] for record in kept]
    queued = [{"kind": kind, "file_path": path, "caption": text} for kind, path, text in zip(kinds, paths, captions)]
    return queued, [{**item, "caption": frozen} for item, frozen in zip(queued, rendered)], kept


TELEGRAM_TYPING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "account_id": ACCOUNT_ID_SCHEMA,
        "chat_id": {"type": "string", "description": "This turn's own Telegram chat ID, as in the event."},
        "topic_id": {"type": "integer", "description": "This turn's forum topic, as in the event; omit outside a forum topic."},
    },
    "required": ["chat_id"],
}


def make_typing_tool(
    api: Any,
    *,
    standalone_account: Callable[[], str] = lambda: "",
    owner_account: Callable[[], str] = lambda: "",
    standalone_configured: Optional[Callable[[], bool]] = None,
    owner_place_binder: Optional[Callable[[str, str, str], dict[str, Any]]] = None,
):
    """Ask the runtime to show "typing…" for the calling turn. The call records a request and contacts no one, so it
    answers ``requested``. Not speech: no outbox row, no receipt, no delivery report."""
    def telegram_typing(
        ctx: Any = None, *, chat_id: str, topic_id: int | None = None, account_id: str = "",
    ) -> dict[str, Any]:
        metadata = getattr(ctx, "task_metadata", None) or {}
        event = (metadata.get("presence") or {}).get("event") or {} if isinstance(metadata, dict) else {}
        turn = str(getattr(ctx, "task_id", "") or "").strip()
        own_chat, own_topic = str(event.get("conversation_id") or ""), str(event.get("thread_id") or "")
        own_account = canonical_bot_id(event.get("account_id"))
        if event.get("provider") != "telegram" or not turn or not own_chat:
            return {"ok": False, "code": "no_turn", "error": "typing shows only during a Telegram Presence turn, in "
                    "its own chat. Nothing was shown."}
        if str(chat_id or "").strip() != own_chat or (topic_id not in (None, "") and str(topic_id) != own_topic):
            where = f"chat {own_chat}" + (f", topic {own_topic}" if own_topic else "")
            return {"ok": False, "code": "other_conversation", "error": f"typing shows only in this turn's {where}. "
                    "Nothing was shown."}
        raw_account = str(account_id or "").strip()
        if raw_account and own_account and raw_account != own_account:
            return {"ok": False, "code": "other_conversation",
                    "error": f"typing shows only under this turn's bot account {own_account}. Nothing was shown."}
        try:
            root = pathlib.Path(api.get_state_dir())
            own = turn_target(root, ctx, own_chat, topic_id=own_topic)
            source = resolve_tool_source(
                root,
                account_id=raw_account,
                ctx=ctx,
                standalone_account=standalone_account,
                owner_account=owner_account,
                standalone_configured=standalone_configured,
                turn=own,
            )
            enforce_source_chat_policy(source, own_chat)
            store = CustodyStore.shared(source.store_path, owner_bot=source.source == SOURCE_OWNER_BOT)
            source_event_id = str(event.get("source_event_id") or "")
            if not store.turn_live(source_event_id, turn):
                return {"ok": False, "code": "turn_ended", "error": "this turn is no longer live here. Nothing was shown."}
            place_binding: Optional[dict[str, Any]] = None
            if source.source == SOURCE_OWNER_BOT:
                # The turn's own event decides: its bot and the place connection it arrived under, never the bot or
                # place active now.
                if own is None or own.binding is None or own.account_id != source.account_id:
                    return {"ok": False, "code": "turn_place_unknown",
                            "error": "this turn's own bot and place connection are not the ones connected here now. "
                                     "Nothing was shown."}
                place_binding = own.binding
        except SendRefused as refusal:
            return refusal.answer()
        # The forum topic of the turn's event, never a reply chain (events.py keeps that as reply_chain_id).
        store.request_typing(
            chat_id=own_chat,
            topic_id=own_topic,
            turn_ref=turn,
            source_event_id=source_event_id,
            place_binding=place_binding,
        )
        return {
            "ok": True,
            "typing": "requested",
            **({"account_id": source.account_id} if source.expose_account else {}),
            "chat_id": own_chat,
            **({"topic_id": int(own_topic)} if own_topic else {}),
            "note": "Nothing was sent yet. The transport shows it while this turn runs and this place may act, until "
                    "your next message here is delivered (one already queued counts: ask again after it), the turn's "
                    "end or about 2 minutes. It is not speech and does not replace a reply.",
        }

    return telegram_typing


MODERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "account_id": ACCOUNT_ID_SCHEMA,
        "action": {
            "type": "string",
            "enum": ["delete_message", "restrict_member", "ban_member", "unban_member"],
        },
        "chat_id": {
            "type": "string",
            "description": "Exact Telegram chat ID from the event facts or an explicitly selected target.",
        },
        "message_id": {
            "type": "integer",
            "description": "Exact message ID for delete_message.",
        },
        "user_id": {
            "type": "integer",
            "description": "Exact actor ID for member operations.",
        },
        "permissions": {
            "type": "object",
            "additionalProperties": {"type": "boolean"},
            "description": "Telegram ChatPermissions for restrict_member; true permissions can lift a restriction.",
        },
        "until_date": {
            "type": "integer",
            "description": "Optional Unix timestamp for ban/restrict expiry. Zero means indefinite; Telegram also treats durations below 30 seconds or over 366 days as indefinite.",
        },
        "revoke_messages": {
            "type": "boolean",
            "description": "Delete the banned member's messages; Telegram always does so in supergroups/channels.",
        },
        "request_id": {
            "type": "string",
            "description": "Stable caller ID; reuse for retries of the same operation.",
        },
    },
    "required": ["action", "chat_id", "request_id"],
}


TELEGRAM_OPERATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "account_id": ACCOUNT_ID_SCHEMA,
        "method": {"type": "string", "enum": ["editMessageText", "setMessageReaction"]},
        "chat_id": {"type": "string", "description": "Exact numeric Telegram chat ID."},
        "message_id": {"type": "integer", "description": "Exact provider message ID."},
        "text": {"type": "string", "description": "Replacement text for editMessageText."},
        "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2"],
                       "description": "Optional text formatting for edits; omit for plain text."},
        "topic_id": {"type": "integer", "description": "Known forum topic of the target. Checked against this topic's place and used for local ordering and history; never sent to Telegram. A reaction in a forum needs it unless the message is in General."},
        "reaction": {"type": "array", "maxItems": 1, "items": {"type": "object", "additionalProperties": True}, "description": "At most one Telegram reaction: {\"type\":\"emoji\",\"emoji\":...} or {\"type\":\"custom_emoji\",\"custom_emoji_id\":...}. It replaces this bot's earlier reaction; [] removes it. Paid reactions are not available to bots."},
        "is_big": {"type": "boolean", "default": False},
        "original_delivery_id": {"type": "string", "description": "Optional prior durable send identity retained as a source reference."},
        "request_id": {"type": "string", "description": "Stable operation identity; reuse only for the same logical mutation."},
    },
    "required": ["method", "chat_id", "message_id", "request_id"],
}


def _positive_id(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def make_moderation_tool(
    api: Any,
    *,
    standalone_account: Callable[[], str] = lambda: "",
    owner_account: Callable[[], str] = lambda: "",
    standalone_configured: Optional[Callable[[], bool]] = None,
    owner_place_binder: Optional[Callable[[str, str, str], dict[str, Any]]] = None,
):
    def telegram_moderate(
        ctx: Any = None,
        *,
        action: str,
        chat_id: str,
        request_id: str,
        account_id: str = "",
        message_id: int = 0,
        user_id: int = 0,
        permissions: dict[str, bool] | None = None,
        until_date: int = 0,
        revoke_messages: bool = False,
    ) -> dict[str, Any]:
        try:
            root = pathlib.Path(api.get_state_dir())
            # Deleting this turn's own message acts on that event, under the bot and connection it came through.
            turn = turn_target(root, ctx, chat_id, message_id=message_id) if action == "delete_message" else None
            source = resolve_tool_source(
                root,
                account_id=account_id,
                ctx=ctx,
                standalone_account=standalone_account,
                owner_account=owner_account,
                standalone_configured=standalone_configured,
                turn=turn,
            )
            target = str(chat_id).strip()
            if re.fullmatch(r"-?[0-9]+", target) is None:
                raise ValueError("chat_id must be an exact numeric Telegram id")
            enforce_source_chat_policy(source, target)
            if not str(request_id).strip():
                raise ValueError("request_id is required for a retryable operation")
            parameters: dict[str, Any] = {"chat_id": target}
            if action == "delete_message":
                parameters["message_id"] = _positive_id(message_id, "message_id")
            elif action in {"restrict_member", "ban_member", "unban_member"}:
                parameters["user_id"] = _positive_id(user_id, "user_id")
                if action == "restrict_member":
                    if (
                        not isinstance(permissions, dict)
                        or not permissions
                        or any(
                            type(value) is not bool for value in permissions.values()
                        )
                    ):
                        raise ValueError(
                            "permissions must be a nonempty Telegram ChatPermissions object of boolean values"
                        )
                    parameters.update(
                        permissions=dict(permissions),
                        use_independent_chat_permissions=True,
                    )
                if action in {"restrict_member", "ban_member"}:
                    if isinstance(until_date, bool) or int(until_date) < 0:
                        raise ValueError(
                            "until_date must be a nonnegative Unix timestamp"
                        )
                    parameters["until_date"] = int(until_date)
                if action == "ban_member":
                    parameters["revoke_messages"] = bool(revoke_messages)
                if action == "unban_member":
                    parameters["only_if_banned"] = True
            else:
                raise ValueError("unsupported Telegram moderation action")
            store = CustodyStore.shared(source.store_path, owner_bot=source.source == SOURCE_OWNER_BOT)
            # Deleting a known message acts in its own topic's place; member actions act on the whole group, so they
            # need the group's own place (no topic).
            local_topic = ""
            if source.source == SOURCE_OWNER_BOT and action == "delete_message":
                if turn is not None and turn.binding is not None and turn.account_id == source.account_id:
                    local_topic = turn.binding["thread_id"]
                else:
                    known = store.reaction_target(source.account_id, target, parameters["message_id"])
                    if known.get("retained") and known.get("topic_id"):
                        local_topic = str(known["topic_id"])
            place_binding = turn_place_or_bind(
                source,
                turn,
                target,
                topic_id=local_topic,
                owner_place_binder=owner_place_binder,
            )
        except SendRefused as refusal:
            return refusal.answer()
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        receipt = str(request_id).strip()
        payload: dict[str, Any] = {
            "kind": "moderation",
            "chat_id": target,
            "action": action,
            "parameters": parameters,
        }
        if source.pin_account:
            payload["account_id"] = source.account_id
        if local_topic:
            payload["topic_id"] = local_topic
        if place_binding is not None:
            payload["_place_binding"] = place_binding
        inserted = store.enqueue_outbox(
            f"telegram-moderate:{receipt}",
            payload,
        )
        return {
            "ok": True,
            "state": "queued" if inserted else "already_queued",
            "request_id": receipt,
            "action": action,
            **({"account_id": source.account_id} if source.expose_account else {}),
        }

    return telegram_moderate


def _receipt_matches_account(receipt: dict[str, Any], account_id: str) -> bool:
    reporting = receipt.get("delivery_reporting")
    recorded = canonical_bot_id(receipt.get("account_id")) or canonical_bot_id(
        reporting.get("account_id") if isinstance(reporting, dict) else ""
    )
    return bool(recorded and recorded == account_id)


def make_receipt_tool(api: Any):
    def telegram_receipt(ctx=None, *, request_id: str, operation: str = "send", account_id: str = "") -> dict[str, Any]:
        if operation not in {"send", "moderate", "operation"}:
            return {"ok": False, "error": "operation must be send, moderate, or operation"}
        root = pathlib.Path(api.get_state_dir())
        delivery_id = f"telegram-{operation}:{str(request_id).strip()}"
        standalone_store = root / "custody.sqlite3"
        raw_account = str(account_id or "").strip() if account_id is not None else ""
        if not raw_account:
            owner_turn = _owner_turn(root, ctx)
            if owner_turn is None:
                return {"ok": False, "code": "receipt_source_unavailable",
                        "error": "Could not verify this Telegram turn's bot. Pass the known account_id."}
            if owner_turn:
                # A retained turn in this store knows its own bot, even if the same request_id exists in B.
                account = _turn_account_id(ctx)
                receipt = CustodyStore.shared(owner_store_path(root)).delivery_receipt(delivery_id)
                return receipt if _receipt_matches_account(receipt, account) else {"state": "not_found"}
            # Ordinary lookups keep the legacy standalone default, also for rows that named no bot.
            receipt = CustodyStore.shared(standalone_store).delivery_receipt(delivery_id)
            if receipt.get("state") == "not_found" and owner_store_exists(root):
                receipt = CustodyStore.shared(owner_store_path(root)).delivery_receipt(delivery_id)
            return receipt
        req = canonical_bot_id(raw_account)
        if not req:
            return {"ok": False, "code": "invalid_account_id", "error": "account_id must be a positive numeric Telegram bot id"}

        # The exact receipt must identify the bot; current roles cannot disambiguate historical rows.
        matches = []
        try:
            for path in (standalone_store, owner_store_path(root)):
                if path.is_file():
                    receipt = CustodyStore.shared(path).delivery_receipt(delivery_id)
                    if _receipt_matches_account(receipt, req):
                        matches.append(receipt)
        except (OSError, sqlite3.Error, ValueError):
            return {"ok": False, "code": "receipt_source_unavailable",
                    "error": "Could not verify the source of this receipt. Try reading it again when the delivery history is available."}
        if len(matches) > 1:
            return {"ok": False, "code": "receipt_source_ambiguous",
                    "error": "This account and request ID match receipts in both Telegram roles. No receipt was selected."}
        return matches[0] if matches else {"state": "not_found"}

    return telegram_receipt


def _reaction_values_or_refuse(reaction: Any) -> list[dict[str, Any]]:
    """Telegram's own limits for a bot's reaction; which emoji a chat allows stays Telegram's answer."""
    if not isinstance(reaction, list) or any(not isinstance(item, dict) for item in reaction):
        raise ValueError("reaction must be a list of Telegram reaction objects")
    if len(reaction) > 1:
        raise ValueError("a Telegram bot can set at most one reaction per message; [] removes it")
    for item in reaction:
        if item.get("type") == "paid":
            raise ValueError("Telegram does not let bots set paid reactions")
        if not (item.get("type") == "emoji" and item.get("emoji")) and not (
                item.get("type") == "custom_emoji" and item.get("custom_emoji_id")):
            raise ValueError("a reaction is {type: emoji, emoji} or {type: custom_emoji, custom_emoji_id}")
    return [dict(item) for item in reaction]


def _reaction_conversation(known: dict[str, Any], chat_id: str, message_id: int,
                           topic_id: int | None) -> tuple[str, str]:
    """(topic, target resolution), or a refusal that says exactly what is missing.

    The guard saw ``topic_id`` as given, so a forum reaction without its topic would have been
    allowed by the whole chat's permission instead of that topic's. Explicit topics are only
    checked here and stay local. A positive chat id is a private chat, one conversation unless
    its retained messages have shown topics; since a private chat's records cannot show that it
    has none, an explicit topic there stays possible and unproven.
    """
    given = str(topic_id) if topic_id is not None else ""
    private = not chat_id.startswith("-")
    if known.get("unconfirmed"):
        # No record can be read as this bot's, including those that would prove a private chat's conversation.
        if given or private:
            return given, "unresolved"
        raise ValueError(f"forum_topic_unknown: this bot's identity is unconfirmed for the current token, so its "
                         f"records of chat {chat_id} cannot be read and the topic of message {message_id} is unknown; "
                         "pass the topic_id from the event if you have it, or retry once the transport status shows "
                         "the bot. Not sent.")
    if known["retained"]:
        if given != known["topic_id"]:
            where = f"topic {known['topic_id']}" if known["topic_id"] else "this chat's General conversation"
            raise ValueError(f"forum_topic_mismatch: message {message_id} is in {where}; "
                             f"pass topic_id={known['topic_id'] or 'nothing'}")
        return given, "resolved"
    if known["forum"] == 0 or (known["forum"] is None and private):
        if given and not private:
            raise ValueError("this chat has no topics; omit topic_id")
        return given, "unresolved" if given else "resolved"
    if not given:
        what = ("has shown topics and message" if private else "is a forum and message" if known["forum"] == 1
                else "may be a forum (not observed yet) and message")
        raise ValueError(f"forum_topic_unknown: chat {chat_id} {what} {message_id} is not retained here, so its "
                         "topic is unknown; pass the topic_id from the event if you have it. Not sent.")
    return given, "unresolved"


def make_operation_tool(
    api: Any,
    confirmed_account: Callable[[], str] = lambda: "",
    *,
    owner_account: Callable[[], str] = lambda: "",
    standalone_configured: Optional[Callable[[], bool]] = None,
    owner_place_binder: Optional[Callable[[str, str, str], dict[str, Any]]] = None,
):
    def telegram_operation(
        ctx: Any = None,
        *, method: str, chat_id: str, message_id: int, request_id: str,
        account_id: str = "",
        text: str = "", parse_mode: str = "", topic_id: int | None = None,
        reaction: list[dict[str, Any]] | None = None, is_big: bool = False,
        original_delivery_id: str = "",
    ) -> dict[str, Any]:
        try:
            root = pathlib.Path(api.get_state_dir())
            # Actions on the turn's own message keep its bot and connection. An edit of another message is bound now.
            turn = turn_target(root, ctx, chat_id, message_id=message_id)
            source = resolve_tool_source(
                root,
                account_id=account_id,
                ctx=ctx,
                standalone_account=confirmed_account,
                owner_account=owner_account,
                standalone_configured=standalone_configured,
                turn=turn,
            )
            if method not in {"editMessageText", "setMessageReaction"}:
                raise ValueError("unsupported Telegram operation")
            target = str(chat_id).strip()
            if re.fullmatch(r"-?[0-9]+", target) is None:
                raise ValueError("chat_id must be an exact numeric Telegram id")
            enforce_source_chat_policy(source, target)
            message = _positive_id(message_id, "message_id")
            receipt = str(request_id or "").strip()
            if not receipt:
                raise ValueError("request_id is required for a retryable operation")
            parameters: dict[str, Any] = {"chat_id": target, "message_id": message}
            local_topic_id: Any = _positive_id(topic_id, "topic_id") if topic_id is not None else None
            store = CustodyStore.shared(source.store_path, owner_bot=source.source == SOURCE_OWNER_BOT)
            reaction_target, album = None, ""
            if method == "editMessageText":
                if not str(text or "").strip():
                    raise ValueError("text is required for editMessageText")
                parameters["text"] = str(text)
                if parse_mode:
                    parameters["parse_mode"] = str(parse_mode)
                if source.source == SOURCE_OWNER_BOT:
                    # The owner's bot edits a known message under the place of the topic it is in, never another's.
                    known = store.reaction_target(source.account_id, target, message)
                    if known.get("retained"):
                        held_topic = str(known.get("topic_id") or "")
                        if local_topic_id is not None and str(local_topic_id) != held_topic:
                            where = f"topic {held_topic}" if held_topic else "this chat's General conversation"
                            raise ValueError(f"forum_topic_mismatch: message {message} is in {where}; "
                                             f"pass topic_id={held_topic or 'nothing'}")
                        local_topic_id = held_topic or None
            else:
                parameters["reaction"] = _reaction_values_or_refuse(reaction)
                parameters["is_big"] = bool(is_big)
                account = source.account_id
                known = store.reaction_target(account, target, message) if account else {"unconfirmed": True}
                local_topic_id, resolution = _reaction_conversation(known, target, message, local_topic_id)
                reaction_target = {"message_id": str(message), "resolution": resolution}
                album = known.get("media_group_id", "")
            place_binding = turn_place_or_bind(
                source,
                turn,
                target,
                topic_id=local_topic_id,
                owner_place_binder=owner_place_binder,
            )
        except SendRefused as refusal:
            return refusal.answer()
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        origin = tool_origin(ctx)
        reporting: dict[str, Any] = {"version": -1, "origin": origin}
        if source.pin_account:
            reporting["account_id"] = source.account_id
        payload = {
            "kind": "operation", "chat_id": target, "method": method,
            "parameters": parameters, "text": str(text or ""),
            "_reporting": reporting,
        }
        if source.pin_account:
            payload["account_id"] = source.account_id
        if place_binding is not None:
            payload["_place_binding"] = place_binding
        if reaction_target:
            payload["_reaction_target"] = reaction_target
        if album:
            payload["_reaction_album"] = album
        if original_delivery_id:
            payload["original_delivery_id"] = str(original_delivery_id)
        if local_topic_id not in (None, ""):
            # Telegram's edit and reaction endpoints address the message directly;
            # keep the known topic only as a local ordering and history fact, never
            # as an unsupported Bot API argument.
            payload["topic_id"] = local_topic_id
        inserted = store.enqueue_outbox(
            f"telegram-operation:{receipt}", payload,
        )
        return {
            "ok": True,
            "state": "queued" if inserted else "already_queued",
            "request_id": receipt,
            "method": method,
            "chat_id": target,
            "message_id": message,
            **({"account_id": source.account_id} if source.expose_account else {}),
        }

    return telegram_operation


TELEGRAM_REACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "account_id": ACCOUNT_ID_SCHEMA,
        "action": {"type": "string", "enum": ["set", "clear"],
                   "description": "set puts this bot's one reaction on the message, replacing its earlier one; clear removes it."},
        "chat_id": {"type": "string", "description": "Exact numeric Telegram chat ID."},
        "message_id": {"type": "integer", "description": "Exact provider message ID of the message reacted to."},
        "topic_id": {"type": "integer", "description": "Known forum topic of the target, as in the event. Checked against this topic's place and kept local; never sent to Telegram. A reaction in a forum needs it unless the message is in General."},
        "emoji": {"type": "string", "description": "For set: one emoji Telegram offers for reactions, such as 👍 or 👀."},
        "custom_emoji_id": {"type": "string", "description": "For set, instead of emoji: a custom emoji ID."},
        "is_big": {"type": "boolean", "default": False},
        "request_id": {"type": "string", "description": "Stable operation identity; reuse only for the same logical reaction. Its receipt is telegram_receipt(operation=operation)."},
    },
    "required": ["action", "chat_id", "message_id", "request_id"],
}


def make_react_tool(operation: Any):
    """``telegram_operation(method=setMessageReaction)`` with set/clear arguments: the same checks, outbox and receipt."""
    def telegram_react(
        ctx: Any = None,
        *, action: str, chat_id: str, message_id: int, request_id: str,
        account_id: str = "",
        topic_id: int | None = None, emoji: str = "", custom_emoji_id: str = "", is_big: bool = False,
    ) -> dict[str, Any]:
        emoji, custom = str(emoji or "").strip(), str(custom_emoji_id or "").strip()
        if action == "set":
            if bool(emoji) == bool(custom):
                return {"ok": False, "error": "set takes exactly one of emoji or custom_emoji_id"}
            reaction = [{"type": "emoji", "emoji": emoji} if emoji else {"type": "custom_emoji", "custom_emoji_id": custom}]
        elif action == "clear":
            if emoji or custom:
                return {"ok": False, "error": "clear removes this bot's reaction; omit emoji and custom_emoji_id"}
            reaction = []
        else:
            return {"ok": False, "error": "action must be set or clear"}
        kwargs: dict[str, Any] = {
            "method": "setMessageReaction",
            "chat_id": chat_id,
            "message_id": message_id,
            "request_id": request_id,
            "topic_id": topic_id,
            "reaction": reaction,
            "is_big": is_big,
        }
        if account_id:
            kwargs["account_id"] = account_id
        return operation(ctx, **kwargs)

    return telegram_react
