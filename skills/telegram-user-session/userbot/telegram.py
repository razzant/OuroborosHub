"""Narrow Telethon user-account boundary; session files and secrets belong to the caller.

Only the companion owns the client/session. Its durable outbox must persist a random_id
BEFORE calling send_text and must not blindly retry an ambiguous result. Topic catalog
membership and inbound forum classification live in userbot.topic, not here.
Telethon is imported on use, so importing the skill/plugin does not require its isolated
companion dependency to be present in the host process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LoginRequired(Exception):
    """A session is not authorized; caller must explicitly conduct login."""


class FloodWait(Exception):
    """Telegram declined this attempt until retry_after seconds have elapsed."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Telegram flood wait: {retry_after}s")


class AmbiguousSend(Exception):
    """A send may have reached Telegram; reconcile it rather than minting a new ID."""


class SendRejected(Exception):
    """A definite Telegram RPC refusal; no successful send was acknowledged."""


@dataclass(frozen=True)
class SendResult:
    random_id: int
    message_id: int | None


class TelegramAdapter:
    """Async MTProto facade for an exclusively owned user session.

    session_path is caller-chosen storage *outside* the shipped source tree. Supplying
    client enables offline tests; normal construction lazily creates TelegramClient.
    Do not share the same session among companion processes or IPs.
    """

    def __init__(
        self,
        session_path: str | Path,
        api_id: int,
        api_hash: str,
        *,
        client: Any = None,
    ) -> None:
        self.session_path = str(session_path)
        self._client = client
        self._api_id = api_id
        self._api_hash = api_hash

    @property
    def client(self) -> Any:
        if self._client is None:
            from telethon import TelegramClient

            # No implicit long flood sleep or transport replay of a mutation. The
            # durable caller decides what to do with failed/uncertain attempts.
            self._client = TelegramClient(
                self.session_path,
                self._api_id,
                self._api_hash,
                request_retries=0,
                flood_sleep_threshold=0,
            )
        return self._client

    async def connect(self) -> bool:
        """Connect without an interactive sign-in; return whether session is authorized."""
        await self.client.connect()
        return await self.client.is_user_authorized()

    async def authorization_status(self) -> bool:
        """Return the current session authorization status (connect first)."""
        return await self.client.is_user_authorized()

    async def get_me(self) -> int:
        """Return the authenticated USER account ID; reject bot sessions."""
        me = await self.client.get_me()
        if me is None:
            raise LoginRequired("Telegram session is not authorized")
        if getattr(me, "bot", False):
            raise ValueError("a user account, not a bot token, is required")
        return int(me.id)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    async def request_login_code(self, phone: str, *, resend_hash: str | None = None) -> str:
        """One explicit code request/resend. Metadata is not proof of human delivery."""
        if resend_hash:
            from telethon.tl.functions.auth import ResendCodeRequest
            sent = await self.client(ResendCodeRequest(phone, resend_hash))
        else:
            sent = await self.client.send_code_request(phone)
        self.code_delivery = {'type':type(getattr(sent,'type',None)).__name__,
                              'next_type':type(getattr(sent,'next_type',None)).__name__,
                              'timeout':getattr(sent,'timeout',None)}
        return sent.phone_code_hash

    async def complete_login(
        self,
        phone: str,
        code: str,
        phone_code_hash: str,
        *,
        password: str | None = None,
    ) -> bool:
        """Complete code login, optionally using 2FA password if Telegram requires it.

        A missing required password propagates Telethon's SessionPasswordNeededError;
        the caller may ask the human and call complete_password. Password is not
        stored here or passed to send_code_request. If supplied in this call,
        password is used only after the code elicits a 2FA challenge.
        """
        from telethon.errors import SessionPasswordNeededError

        try:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if password is None:
                raise
            await self.client.sign_in(password=password)
        return await self.client.is_user_authorized()

    async def complete_password(self, password: str) -> bool:
        """Resume a previously challenged 2FA login without replaying its code."""
        if not password:
            raise ValueError("password must be nonempty")
        await self.client.sign_in(password=password)
        return await self.client.is_user_authorized()

    async def send_text(
        self,
        peer_id: int,
        text: str,
        *,
        random_id: int,
        topic_id: int | None = None,
        reply_id: int | None = None,
        before_mutation=None,
    ) -> SendResult:
        """Send one plain-text message using caller-persisted random_id.

        None/1 topic means General, where an ordinary reply may be specified. A
        non-General topic uses its root as reply_to_msg_id; a nested reply uses
        reply_id AND top_msg_id=topic_id, even if that reply is later deleted.
        Caller must verify forum status and topic catalog membership beforehand.
        A transport timeout/disconnect after invoking the request is ambiguous and
        must be reconciled with the same durable intent, never retried with a new ID.
        """
        from telethon import errors
        from telethon.tl.functions.messages import SendMessageRequest
        from telethon.tl.types import InputReplyToMessage

        if isinstance(peer_id, bool) or not isinstance(peer_id, int) or peer_id == 0:
            raise ValueError("peer_id must be a nonzero Telegram peer ID")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be nonempty")
        if isinstance(random_id, bool) or not isinstance(random_id, int) or not (-2**63 <= random_id < 2**63):
            raise ValueError("random_id must be a signed 64-bit integer persisted by the caller")
        for name, value in (("topic_id", topic_id), ("reply_id", reply_id)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not (0 < value < 2**31)):
                raise ValueError(f"{name} must be a positive 32-bit message ID")
        if reply_id is not None and topic_id not in (None, 1) and reply_id == topic_id:
            # Reply to a topic root has no nested top_msg_id: it is the root send.
            reply_id = None

        reply_to = None
        if topic_id not in (None, 1):
            reply_to = InputReplyToMessage(
                reply_to_msg_id=reply_id or topic_id,
                top_msg_id=topic_id if reply_id is not None else None,
            )
        elif reply_id is not None:
            reply_to = InputReplyToMessage(reply_to_msg_id=reply_id)

        # Resolve before the mutating call: resolution failures have not sent text.
        peer = await self.client.get_input_entity(peer_id)
        if not await self.client.is_user_authorized():
            raise LoginRequired("Telegram session is not authorized")
        request = SendMessageRequest(
            peer=peer, message=text, random_id=random_id, reply_to=reply_to,
        )
        if before_mutation is not None:
            await before_mutation()
        try:
            updates = await self.client(request)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError,
                errors.FloodTestPhoneWaitError, errors.SlowModeWaitError) as exc:
            raise FloodWait(exc.seconds) from exc
        except (errors.TimeoutError, errors.TimedOutError, errors.RandomIdDuplicateError) as exc:
            raise AmbiguousSend("Telegram send outcome is unknown; reconcile random_id") from exc
        except errors.ServerError as exc:
            # A server-side failure (including RpcCallFailError) does not prove
            # that this mutation was not applied before the error was returned.
            raise AmbiguousSend("Telegram server send outcome is unknown") from exc
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            raise AmbiguousSend("Telegram send transport outcome is unknown") from exc
        except errors.RPCError as exc:
            raise SendRejected(str(exc)) from exc
        except Exception as exc:
            # Once __call__ begins, even an unexpected client failure does not
            # prove non-delivery. Keep the intent unresolved rather than resend.
            raise AmbiguousSend("Telegram send outcome is unknown") from exc
        return SendResult(random_id=random_id, message_id=_sent_id(updates, random_id))

    async def recent_messages(
        self, peer_id: int, *, limit: int = 50, min_id: int = 0
    ) -> list[Any]:
        """Read raw Telethon Message objects; caller classifies/deduplicates them."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
            raise ValueError("limit must be between 1 and 100")
        if isinstance(min_id, bool) or not isinstance(min_id, int) or min_id < 0:
            raise ValueError("min_id must be nonnegative")
        peer = await self.client.get_input_entity(peer_id)
        if not await self.client.is_user_authorized():
            raise LoginRequired("Telegram session is not authorized")
        return [message async for message in self.client.iter_messages(peer, limit=limit, min_id=min_id)]

    async def topic_ids(self, peer_id: int, *, guard: Any = None) -> set[int]:
        """Fetch a complete, current forum catalog; refuse non-forum/monoforum peers.

        The returned set contains General (1) for every validated forum. We do
        not return a partial catalog if pagination cannot advance: callers use
        this set to reject unknown outgoing topics and classify inbound threads.
        Caller should refresh it when Telegram reports a moved/deleted topic.
        ``guard``, when given, runs before each Telegram request here, every
        catalog page included, and may raise to stop the read between them.
        """
        from telethon.tl import types
        from telethon.tl.functions.messages import GetForumTopicsRequest

        check = guard or (lambda: None)
        check()
        peer = await self.client.get_input_entity(peer_id)
        check()
        if not await self.client.is_user_authorized():
            raise LoginRequired("Telegram session is not authorized")
        check()
        channel = await self.client.get_entity(peer)
        if (not isinstance(channel, types.Channel) or not channel.megagroup
                or not channel.forum or channel.monoforum):
            raise ValueError("peer must be a forum supergroup, not a monoforum")

        ids = {1}
        listed_ids: set[int] = set()
        cursor_date = None
        cursor_message = cursor_topic = 0
        seen_cursors: set[tuple[int, int]] = set()
        # A server can return endlessly advancing pages; never monopolize the
        # companion beyond one bounded catalog operation or return partial data.
        for _ in range(100):
            check()
            result = await self.client(GetForumTopicsRequest(
                peer=peer,
                offset_date=cursor_date,
                offset_id=cursor_message,
                offset_topic=cursor_topic,
                limit=100,
            ))
            topics = result.topics
            if not topics:
                if len(listed_ids) < result.count:
                    raise RuntimeError("Telegram returned an incomplete forum catalog")
                return ids
            for topic in topics:
                if isinstance(topic, types.ForumTopic):
                    ids.add(topic.id)
                    listed_ids.add(topic.id)
            if len(listed_ids) >= result.count:
                return ids

            # Telegram paginates by last topic ID and its last message/date,
            # not by list index. A short page is not necessarily the last page.
            last = topics[-1]
            if not isinstance(last, types.ForumTopic):
                raise RuntimeError("cannot paginate a deleted forum topic")
            cursor_topic, cursor_message = last.id, last.top_message
            cursor = (cursor_topic, cursor_message)
            if cursor in seen_cursors:
                raise RuntimeError("Telegram forum catalog pagination did not advance")
            seen_cursors.add(cursor)
            last_message = next(
                (message for message in result.messages if message.id == cursor_message),
                None,
            )
            if result.order_by_create_date:
                cursor_date = last.date
            elif last_message is not None and last_message.date is not None:
                cursor_date = last_message.date
            else:
                raise RuntimeError("cannot paginate forum catalog without last-message date")
        raise RuntimeError("forum catalog exceeds 100-page operation limit")


def _sent_id(updates: Any, random_id: int) -> int | None:
    """Extract a remote ID from the documented Updates variants, if furnished."""
    from telethon.tl import types

    if isinstance(updates, (types.UpdateShortSentMessage, types.UpdateShortMessage, types.UpdateShortChatMessage)):
        return updates.id
    for update in getattr(updates, "updates", ()):
        if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
            return update.id
    # UpdatesTooLong or an unrecognized variant is acknowledged but has no ID.
    return None
