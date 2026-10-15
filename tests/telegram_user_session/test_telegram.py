"""Offline adapter tests: the fake client never opens a socket or authenticates."""

import asyncio
import builtins
import importlib
import unittest
from unittest import mock
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import errors
from telethon.tl import types
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.functions.messages import GetForumTopicsRequest

from userbot.telegram import (
    AmbiguousSend,
    FloodWait,
    LoginRequired,
    SendRejected,
    TelegramAdapter,
    _sent_id,
)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.result = types.UpdateShortSentMessage(id=73, pts=1, pts_count=1, date=None)
        self.authorized = True
        self.connected = False
        self.peer = object()
        self.me = SimpleNamespace(id=1024, bot=False)
        self.entity = types.Channel(
            id=123, title="forum", photo=types.ChatPhotoEmpty(), date=None,
            megagroup=True, forum=True,
        )

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_input_entity(self, peer_id):
        self.calls.append(("peer", peer_id))
        return self.peer

    async def __call__(self, request):
        self.calls.append(("send", request))
        if isinstance(request, GetForumTopicsRequest):
            return self.pages.pop(0)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def get_entity(self, peer):
        self.calls.append(("entity", peer))
        return self.entity

    async def get_me(self):
        return self.me

    async def send_code_request(self, phone):
        self.calls.append(("code", phone))
        return SimpleNamespace(phone_code_hash="hash-from-telegram")

    async def sign_in(self, **kwargs):
        self.calls.append(("sign_in", kwargs))

    async def iter_messages(self, peer, *, limit, min_id):
        self.calls.append(("read", peer, limit, min_id))
        for item in (SimpleNamespace(id=12), SimpleNamespace(id=11)):
            yield item


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.adapter = TelegramAdapter("outside-repo/session", 123, "fake-hash", client=self.fake)

    async def test_connect_status_and_explicit_login(self):
        self.fake.authorized = False
        self.assertFalse(await self.adapter.connect())
        self.assertTrue(self.fake.connected)
        self.assertEqual(await self.adapter.request_login_code("+123"), "hash-from-telegram")
        self.fake.authorized = True
        self.assertTrue(await self.adapter.complete_login("+123", "12345", "hash-from-telegram"))
        self.assertEqual(self.fake.calls[-1], ("sign_in", {
            "phone": "+123", "code": "12345", "phone_code_hash": "hash-from-telegram"
        }))
        await self.adapter.disconnect()
        self.assertFalse(self.fake.connected)

    async def test_second_factor_is_explicit_separate_step(self):
        self.fake.authorized = False
        def challenged(**kwargs):
            self.fake.calls.append(("sign_in", kwargs))
            if "code" in kwargs:
                raise errors.SessionPasswordNeededError(None)
            self.fake.authorized = True
        async def sign_in(**kwargs):
            challenged(**kwargs)
        self.fake.sign_in = sign_in
        with self.assertRaises(errors.SessionPasswordNeededError):
            await self.adapter.complete_login("+123", "12345", "hash")
        self.assertTrue(await self.adapter.complete_password("2fa"))
        self.assertEqual(self.fake.calls[-1], ("sign_in", {"password": "2fa"}))

    async def test_get_me_rejects_bots_and_unauthorized_sessions(self):
        self.assertEqual(await self.adapter.get_me(), 1024)
        self.fake.me = SimpleNamespace(id=1024, bot=True)
        with self.assertRaisesRegex(ValueError, "user account"):
            await self.adapter.get_me()
        self.fake.me = None
        with self.assertRaises(LoginRequired):
            await self.adapter.get_me()

    async def test_topic_ids_validates_forum_and_paginates_short_pages(self):
        date = datetime(2026, 10, 12, tzinfo=timezone.utc)
        def topic(topic_id, last_id):
            return types.ForumTopic(
                id=topic_id, date=date, peer=types.PeerChannel(123), title="topic",
                icon_color=0, top_message=last_id, read_inbox_max_id=0,
                read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                unread_reactions_count=0, unread_poll_votes_count=0,
                from_id=types.PeerChannel(123), notify_settings=types.PeerNotifySettings(),
            )
        def page(topics):
            return types.messages.ForumTopics(
                count=3, topics=topics,
                messages=[SimpleNamespace(id=t.top_message, date=date) for t in topics],
                chats=[], users=[], pts=1,
            )
        self.fake.pages = [page([topic(1, 10), topic(55, 70)]), page([topic(99, 111)])]
        self.assertEqual(await self.adapter.topic_ids(-100123), {1, 55, 99})
        calls = [req for kind, req in self.fake.calls if kind == "send"]
        self.assertTrue(all(isinstance(req, GetForumTopicsRequest) for req in calls))
        self.assertEqual((calls[0].offset_id, calls[0].offset_topic), (0, 0))
        self.assertEqual((calls[1].offset_id, calls[1].offset_topic, calls[1].offset_date), (70, 55, date))

    async def test_topic_ids_rejects_nonforum_and_incomplete_catalog(self):
        self.fake.entity.forum = False
        with self.assertRaisesRegex(ValueError, "forum supergroup"):
            await self.adapter.topic_ids(-100123)
        self.fake.entity.forum = True
        self.fake.entity.monoforum = True
        with self.assertRaisesRegex(ValueError, "monoforum"):
            await self.adapter.topic_ids(-100123)
        self.fake.entity.monoforum = False
        self.fake.pages = [types.messages.ForumTopics(
            count=2, topics=[], messages=[], chats=[], users=[], pts=1,
        )]
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            await self.adapter.topic_ids(-100123)

    async def test_topic_catalog_refuses_endlessly_advancing_pages(self):
        date = datetime(2026, 10, 12, tzinfo=timezone.utc)
        pages = []
        for number in range(1, 101):
            topic = types.ForumTopic(
                id=number + 1, date=date, peer=types.PeerChannel(123), title="topic",
                icon_color=0, top_message=number + 200,
                read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
                unread_mentions_count=0, unread_reactions_count=0,
                unread_poll_votes_count=0, from_id=types.PeerChannel(123),
                notify_settings=types.PeerNotifySettings())
            pages.append(types.messages.ForumTopics(
                count=101, topics=[topic],
                messages=[SimpleNamespace(id=topic.top_message, date=date)],
                chats=[], users=[], pts=1))
        self.fake.pages = pages
        with self.assertRaisesRegex(RuntimeError, "100-page operation limit"):
            await self.adapter.topic_ids(-100123)
        self.assertEqual(sum(kind == "send" for kind, _ in self.fake.calls), 100)

    async def test_non_general_root_send_uses_root_without_top(self):
        result = await self.adapter.send_text(-100123, "Hello", random_id=9, topic_id=55)
        req = self.fake.calls[-1][1]
        self.assertIsInstance(req, SendMessageRequest)
        self.assertIs(req.peer, self.fake.peer)
        self.assertEqual((req.message, req.random_id), ("Hello", 9))
        self.assertIsInstance(req.reply_to, types.InputReplyToMessage)
        self.assertEqual((req.reply_to.reply_to_msg_id, req.reply_to.top_msg_id), (55, None))
        self.assertEqual((result.random_id, result.message_id), (9, 73))

    async def test_non_general_nested_reply_carries_top_even_when_original_is_gone(self):
        await self.adapter.send_text(-100123, "answer", random_id=-12, topic_id=55, reply_id=77)
        req = self.fake.calls[-1][1]
        self.assertEqual((req.reply_to.reply_to_msg_id, req.reply_to.top_msg_id), (77, 55))
        await self.adapter.send_text(-100123, "root", random_id=-12, topic_id=55, reply_id=55)
        req = self.fake.calls[-1][1]
        self.assertEqual((req.reply_to.reply_to_msg_id, req.reply_to.top_msg_id), (55, None))

    async def test_general_has_no_topic_reply_but_supports_ordinary_reply(self):
        for topic in (None, 1):
            await self.adapter.send_text(123, "general", random_id=4, topic_id=topic)
            self.assertIsNone(self.fake.calls[-1][1].reply_to)
            await self.adapter.send_text(123, "reply", random_id=4, topic_id=topic, reply_id=19)
            reply = self.fake.calls[-1][1].reply_to
            self.assertEqual((reply.reply_to_msg_id, reply.top_msg_id), (19, None))

    async def test_validation_prevents_mutating_call(self):
        for kwargs in (
            {"random_id": True}, {"random_id": 2**63},
            {"random_id": 1, "topic_id": -1},
            {"random_id": 1, "reply_id": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.adapter.send_text(-100123, "hi", **kwargs)
        self.assertEqual(self.fake.calls, [])

    async def test_unauthorized_cannot_send_and_read_is_explicit(self):
        self.fake.authorized = False
        with self.assertRaises(LoginRequired):
            await self.adapter.send_text(123, "Hi", random_id=55)
        self.assertEqual([kind for kind, *_ in self.fake.calls if kind == "send"], [])
        self.fake.authorized = True
        messages = await self.adapter.recent_messages(123, limit=2, min_id=10)
        self.assertEqual([m.id for m in messages], [12, 11])
        self.assertEqual(self.fake.calls[-1], ("read", self.fake.peer, 2, 10))

    async def test_flood_wait_and_ambiguous_errors_are_separate(self):
        for wait_error in (errors.FloodWaitError, errors.SlowModeWaitError):
            self.fake.result = wait_error(request=None, capture=13)
            with self.subTest(wait_error=wait_error), self.assertRaises(FloodWait) as caught:
                await self.adapter.send_text(123, "hi", random_id=1)
            self.assertEqual(caught.exception.retry_after, 13)
        for error in (asyncio.TimeoutError(), OSError("closed"), errors.RandomIdDuplicateError(None)):
            self.fake.result = error
            with self.subTest(error=error), self.assertRaises(AmbiguousSend):
                await self.adapter.send_text(123, "hi", random_id=1)
        self.fake.result = errors.MessageEmptyError(None)
        with self.assertRaises(SendRejected):
            await self.adapter.send_text(123, "hi", random_id=1)

    async def test_server_errors_after_send_are_ambiguous_not_rejected(self):
        for error in (errors.ServerError(None, "server failed"),
                      errors.RpcCallFailError(None)):
            self.fake.result = error
            with self.subTest(error=type(error).__name__), self.assertRaises(AmbiguousSend):
                await self.adapter.send_text(123, "hi", random_id=918)
        self.assertEqual(len([call for call in self.fake.calls if call[0] == "send"]), 2)

    async def test_remote_id_from_matching_random_id_not_unrelated_update(self):
        updates = types.Updates(
            updates=[types.UpdateMessageID(id=42, random_id=9), types.UpdateMessageID(id=8, random_id=10)],
            users=[], chats=[], date=None, seq=1,
        )
        self.assertEqual(_sent_id(updates, 10), 8)
        self.assertIsNone(_sent_id(updates, 11))
        self.assertIsNone(_sent_id(types.UpdatesTooLong(), 9))

    async def test_raw_tl_response_without_matching_receipt_returns_no_id(self):
        self.fake.result = types.UpdatesTooLong()
        self.assertIsNone((await self.adapter.send_text(123, "hi", random_id=9)).message_id)
        self.fake.result = types.Updates(
            updates=[types.UpdateMessageID(id=77, random_id=10)],
            users=[], chats=[], date=None, seq=1,
        )
        self.assertIsNone((await self.adapter.send_text(123, "hi", random_id=9)).message_id)
        self.fake.result = types.Updates(
            updates=[types.UpdateMessageID(id=77, random_id=9)],
            users=[], chats=[], date=None, seq=1,
        )
        self.assertEqual((await self.adapter.send_text(123, "hi", random_id=9)).message_id, 77)


class LazyImportTests(unittest.TestCase):
    def test_module_import_does_not_import_telethon(self):
        original = builtins.__import__
        def blocked(name, *args, **kwargs):
            if name == "telethon" or name.startswith("telethon."):
                raise AssertionError("Telethon imported at module load")
            return original(name, *args, **kwargs)
        with mock.patch.object(builtins, "__import__", side_effect=blocked):
            importlib.reload(importlib.import_module("userbot.telegram"))
