"""Single-message, forum-scoped Telethon actions for an authenticated user session.

This is an adapter, NOT a Presence tool or owner-authorization decision. The caller
supplies a reviewed owner peer allowlist, an exclusively held TelegramAdapter and a
separate durable intent/reconciliation policy. No login, implicit retry, mass delete,
or unbounded chat export occurs here. A preflight read reduces accidental cross-topic
mutation; Telegram can still change a message between that read and the write.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .telegram import LoginRequired, TelegramAdapter
from .topic import classify_message


class ActionRejected(Exception):
    """Telegram definitively refused a mutation; no success was acknowledged."""


class ActionAmbiguous(Exception):
    """A mutation was invoked, but its effect is unknown; reconcile, do not retry."""


class ActionFloodWait(ActionRejected):
    """Telegram refused the attempt pending a wait; the caller decides when to retry."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Telegram flood wait: {retry_after}s")


def _message_id(value: int, name: str) -> int:
    if type(value) is not int or not 0 < value < 2**31:
        raise ValueError(f"{name} must be a positive Telegram int32 message ID")
    return value


class Actions:
    """Operate only inside refreshed forum catalogs of owner-allowlisted peers.

    ``topic_id`` is REQUIRED on every method, including ``None`` for General.
    Read history scans at most ``limit`` latest peer messages, not an entire topic;
    filtering may return fewer than requested. An unknown/removed topic fails closed.
    """

    def __init__(
        self, adapter: TelegramAdapter, allowed_peers: set[int] | frozenset[int]
    ) -> None:
        if not isinstance(adapter, TelegramAdapter):
            raise TypeError("adapter must be a TelegramAdapter")
        if not isinstance(allowed_peers, (set, frozenset)) or not allowed_peers:
            raise ValueError("allowed_peers must be a nonempty set")
        if any(type(peer) is not int or peer == 0 for peer in allowed_peers):
            raise ValueError("allowed_peers must contain only nonzero numeric peer IDs")
        self.adapter = adapter
        self.allowed_peers = frozenset(allowed_peers)

    async def _scope(self, peer_id: int, topic_id: int | None) -> set[int]:
        if type(peer_id) is not int or peer_id not in self.allowed_peers:
            raise ValueError("peer is not owner-allowlisted")
        if topic_id is not None:
            _message_id(topic_id, "topic_id")
        # topic_ids checks authorization and actual forum supergroup status, and
        # rejects incomplete catalogs. Refresh for every action: no stale allowlist.
        catalog = await self.adapter.topic_ids(peer_id)
        if topic_id is not None and topic_id not in catalog:
            raise ValueError("unknown forum topic; refresh the catalog")
        return catalog

    async def history(
        self, peer_id: int, *, topic_id: int | None, limit: int = 50,
        min_id: int = 0,
    ) -> list[Any]:
        """Return up to 100 raw messages from a bounded peer window, topic-filtered."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if type(min_id) is not int or not 0 <= min_id < 2**31:
            raise ValueError("min_id must be a nonnegative Telegram int32 message ID")
        catalog = await self._scope(peer_id, topic_id)
        messages = await self.adapter.recent_messages(peer_id, limit=limit, min_id=min_id)
        if len(messages) > limit:
            raise RuntimeError("recent_messages exceeded the requested bound")
        return [msg for msg in messages if classify_message(msg, catalog) == _canonical(topic_id)]

    async def _target(
        self, peer_id: int, message_id: int, topic_id: int | None,
    ) -> tuple[Any, Any]:
        from telethon import types, utils
        from telethon.tl.functions.channels import GetMessagesRequest

        _message_id(message_id, "message_id")
        catalog = await self._scope(peer_id, topic_id)
        peer = await self.adapter.client.get_input_entity(peer_id)
        if not isinstance(peer, types.InputPeerChannel):
            raise ValueError("peer must resolve to a forum supergroup channel")
        if not await self.adapter.client.is_user_authorized():
            raise LoginRequired("Telegram session is not authorized")
        channel = utils.get_input_channel(peer)
        result = await self.adapter.client(GetMessagesRequest(
            channel=channel, id=[types.InputMessageID(id=message_id)],
        ))
        matches = [msg for msg in result.messages if isinstance(msg, types.Message) and msg.id == message_id]
        if len(matches) != 1:
            raise ValueError("message not accessible in this forum peer")
        message = matches[0]
        if classify_message(message, catalog) != _canonical(topic_id):
            raise ValueError("message belongs to a different forum topic")
        return peer, message

    async def _own_target(
        self, peer_id: int, message_id: int, topic_id: int | None,
    ) -> tuple[Any, Any]:
        from telethon import types

        peer, message = await self._target(peer_id, message_id, topic_id)
        own_id = await self.adapter.get_me()
        # 'out' alone is not authorship; admins may delete others' messages.
        sender = getattr(message, "from_id", None)
        if (not isinstance(sender, types.PeerUser) or sender.user_id != own_id
                or message.out is not True):
            raise ValueError("only messages authored by this authenticated user can be changed")
        return peer, message

    async def edit(
        self, peer_id: int, message_id: int, text: str, *, topic_id: int | None, before_mutation=None,
    ) -> Any:
        """Edit one own text-only message; an uncertain call is never retried here."""
        from telethon.tl.functions.messages import EditMessageRequest

        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be nonempty")
        peer, message = await self._own_target(peer_id, message_id, topic_id)
        if getattr(message, "media", None) is not None or getattr(message, "entities", None):
            raise ValueError("editing media or formatted text is outside this adapter")
        if before_mutation is not None:
            await before_mutation()
        return await _mutate(self.adapter.client, EditMessageRequest(
            peer=peer, id=message_id, message=text, no_webpage=True,
        ))

    async def delete(
        self, peer_id: int, message_id: int, *, topic_id: int | None, before_mutation=None,
    ) -> Any:
        """Delete one own message with channel-scoped raw request (never a list API)."""
        from telethon import utils
        from telethon.tl.functions.channels import DeleteMessagesRequest

        peer, _ = await self._own_target(peer_id, message_id, topic_id)
        if before_mutation is not None:
            await before_mutation()
        return await _mutate(self.adapter.client, DeleteMessagesRequest(
            channel=utils.get_input_channel(peer), id=[message_id],
        ))

    async def react(
        self, peer_id: int, message_id: int, emoji: str, *, topic_id: int | None, before_mutation=None,
    ) -> Any:
        """Set exactly one ordinary Unicode emoji reaction on one accessible message."""
        from telethon.tl import types
        from telethon.tl.functions.messages import SendReactionRequest

        if not isinstance(emoji, str) or not emoji or len(emoji) > 16 or emoji.isspace():
            raise ValueError("emoji must be one nonempty, short emoji string")
        peer, _ = await self._target(peer_id, message_id, topic_id)
        if before_mutation is not None:
            await before_mutation()
        return await _mutate(self.adapter.client, SendReactionRequest(
            peer=peer, msg_id=message_id,
            reaction=[types.ReactionEmoji(emoticon=emoji)],
            big=False, add_to_recent=False,
        ))


def _canonical(topic_id: int | None) -> int | None:
    return None if topic_id in (None, 1) else topic_id


async def _mutate(client: Any, request: Any) -> Any:
    """After invocation, only a definite RPC refusal can establish no success."""
    from telethon import errors

    try:
        return await client(request)
    except (errors.FloodWaitError, errors.FloodPremiumWaitError,
            errors.FloodTestPhoneWaitError, errors.SlowModeWaitError) as exc:
        raise ActionFloodWait(exc.seconds) from exc
    except (errors.TimeoutError, errors.TimedOutError,
            errors.ServerError, errors.RpcCallFailError,
            asyncio.TimeoutError, asyncio.CancelledError,
            ConnectionError, OSError) as exc:
        raise ActionAmbiguous("Telegram mutation outcome is unknown") from exc
    except errors.RPCError as exc:
        raise ActionRejected(str(exc)) from exc
    except Exception as exc:
        raise ActionAmbiguous("Telegram mutation outcome is unknown") from exc
