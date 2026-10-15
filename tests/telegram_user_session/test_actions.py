"""No Telegram connection: inspect genuine Telethon 1.45 TL request objects."""

import asyncio
import unittest

from telethon import errors
from telethon.tl import types
from telethon.tl.functions.channels import DeleteMessagesRequest, GetMessagesRequest
from telethon.tl.functions.messages import EditMessageRequest, SendReactionRequest

from userbot.actions import Actions, ActionAmbiguous, ActionFloodWait, ActionRejected
from userbot.telegram import TelegramAdapter


PEER = -100123
CHANNEL = 123
TOPIC = 55
OWN = 1024


def message(id=73, *, topic=TOPIC, sender=OWN, out=True, media=None, entities=None):
    header = None if topic is None else types.MessageReplyHeader(
        forum_topic=True, reply_to_top_id=topic, reply_to_msg_id=54,
    )
    return types.Message(
        id=id, peer_id=types.PeerChannel(CHANNEL), from_id=types.PeerUser(sender),
        out=out, message="hello", reply_to=header, media=media, entities=entities,
    )


class FakeClient:
    def __init__(self):
        self.peer = types.InputPeerChannel(CHANNEL, 987)
        self.calls = []
        self.authorized = True
        self.target = message()
        self.result = object()

    async def get_input_entity(self, peer_id):
        self.calls.append(("resolve", peer_id))
        return self.peer

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return types.User(id=OWN, first_name="self")

    async def __call__(self, request):
        self.calls.append(("request", request))
        if isinstance(request, GetMessagesRequest):
            return types.messages.Messages(
                messages=[self.target] if self.target is not None else [],
                topics=[], chats=[], users=[],
            )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.adapter = TelegramAdapter("private/session", 123, "secret", client=self.fake)
        self.catalog = {1, TOPIC}
        self.adapter.topic_ids = self._topics
        self.adapter.recent_messages = self._recent
        self.actions = Actions(self.adapter, {PEER})
        self.scans = []
        self.recent = [message(id=10), message(id=11, topic=None)]
        self.catalog_reads = []

    async def _topics(self, peer):
        self.catalog_reads.append(peer)
        return self.catalog

    async def _recent(self, peer, *, limit, min_id):
        self.scans.append((peer, limit, min_id))
        return self.recent

    def mutations(self):
        return [req for kind, req in self.fake.calls if kind == "request" and not isinstance(req, GetMessagesRequest)]

    async def test_topic_scoped_bounded_history(self):
        self.assertEqual([m.id for m in await self.actions.history(PEER, topic_id=TOPIC, limit=2)], [10])
        self.assertEqual([m.id for m in await self.actions.history(PEER, topic_id=None, limit=2)], [11])
        self.assertEqual(self.scans, [(PEER, 2, 0), (PEER, 2, 0)])
        self.assertEqual(self.catalog_reads, [PEER, PEER])
        with self.assertRaises(TypeError):
            await self.actions.history(PEER, limit=2)
        for limit in (0, 101, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await self.actions.history(PEER, topic_id=TOPIC, limit=limit)

    async def test_unknown_peer_topic_or_cross_topic_never_mutates(self):
        for peer, topic in ((999, TOPIC), (PEER, 99), (PEER, True)):
            with self.subTest(peer=peer, topic=topic), self.assertRaises(ValueError):
                await self.actions.delete(peer, 73, topic_id=topic)
        with self.assertRaisesRegex(ValueError, "different forum topic"):
            await self.actions.edit(PEER, 73, "new", topic_id=None)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.catalog_reads, [PEER, PEER])

    async def test_missing_or_wrong_target_preflight_blocks_mutation(self):
        self.fake.target = None
        with self.assertRaisesRegex(ValueError, "not accessible"):
            await self.actions.react(PEER, 73, "👍", topic_id=TOPIC)
        self.fake.target = message(id=74)
        with self.assertRaisesRegex(ValueError, "not accessible"):
            await self.actions.delete(PEER, 73, topic_id=TOPIC)
        self.assertEqual(self.mutations(), [])

    async def test_edit_and_delete_only_own_messages_and_real_channel_requests(self):
        self.assertIs(await self.actions.edit(PEER, 73, "new", topic_id=TOPIC), self.fake.result)
        lookup, req = [r for kind, r in self.fake.calls if kind == "request"]
        self.assertIsInstance(lookup, GetMessagesRequest)
        self.assertEqual((lookup.channel.channel_id, lookup.id[0].id), (CHANNEL, 73))
        self.assertIsInstance(req, EditMessageRequest)
        self.assertEqual((req.peer.channel_id, req.id, req.message, req.no_webpage), (CHANNEL, 73, "new", True))
        self.assertIs(await self.actions.delete(PEER, 73, topic_id=TOPIC), self.fake.result)
        delete = self.mutations()[-1]
        self.assertIsInstance(delete, DeleteMessagesRequest)
        self.assertEqual((delete.channel.channel_id, delete.id), (CHANNEL, [73]))
        self.assertEqual(self.catalog_reads, [PEER, PEER])

    async def test_own_authorship_is_not_guessed_from_out_or_admin_privilege(self):
        for sender, out in ((999, True), (OWN, False)):
            self.fake.target = message(sender=sender, out=out)
            with self.subTest(sender=sender, out=out), self.assertRaisesRegex(ValueError, "authored"):
                await self.actions.delete(PEER, 73, topic_id=TOPIC)
        self.fake.target = message()
        self.fake.target.from_id = None
        with self.assertRaisesRegex(ValueError, "authored"):
            await self.actions.edit(PEER, 73, "new", topic_id=TOPIC)
        self.assertEqual(self.mutations(), [])

    async def test_edit_does_not_strip_media_or_entities(self):
        self.fake.target = message(media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)))
        with self.assertRaisesRegex(ValueError, "media"):
            await self.actions.edit(PEER, 73, "new", topic_id=TOPIC)
        self.fake.target = message(entities=[types.MessageEntityBold(offset=0, length=2)])
        with self.assertRaisesRegex(ValueError, "formatted"):
            await self.actions.edit(PEER, 73, "new", topic_id=TOPIC)
        self.assertEqual(self.mutations(), [])

    async def test_reaction_is_one_explicit_message_and_emoji(self):
        self.assertIs(await self.actions.react(PEER, 73, "👍", topic_id=TOPIC), self.fake.result)
        req = self.mutations()[-1]
        self.assertIsInstance(req, SendReactionRequest)
        self.assertEqual((req.peer.channel_id, req.msg_id, len(req.reaction)), (CHANNEL, 73, 1))
        self.assertIsInstance(req.reaction[0], types.ReactionEmoji)
        self.assertEqual(req.reaction[0].emoticon, "👍")
        self.assertEqual((req.big, req.add_to_recent), (False, False))
        with self.assertRaises(ValueError):
            await self.actions.react(PEER, 73, "", topic_id=TOPIC)

    async def test_unauthorized_and_nonchannel_preflight_never_mutate(self):
        self.fake.authorized = False
        with self.assertRaisesRegex(Exception, "not authorized"):
            await self.actions.react(PEER, 73, "👍", topic_id=TOPIC)
        self.fake.authorized = True
        self.fake.peer = types.InputPeerUser(123, 987)
        with self.assertRaisesRegex(ValueError, "forum supergroup"):
            await self.actions.delete(PEER, 73, topic_id=TOPIC)
        self.assertEqual(self.mutations(), [])

    async def test_mutation_failure_taxonomy_and_no_retry(self):
        failures = [
            (errors.FloodWaitError(request=None, capture=7), ActionFloodWait),
            (errors.MessageAuthorRequiredError(None), ActionRejected),
            (asyncio.TimeoutError(), ActionAmbiguous),
            (OSError("closed"), ActionAmbiguous),
            (RuntimeError("unknown"), ActionAmbiguous),
        ]
        for failure, expected in failures:
            self.fake.calls.clear()
            self.fake.result = failure
            with self.subTest(error=type(failure).__name__), self.assertRaises(expected) as caught:
                await self.actions.react(PEER, 73, "👍", topic_id=TOPIC)
            self.assertEqual(len(self.mutations()), 1)
            if isinstance(caught.exception, ActionFloodWait):
                self.assertEqual(caught.exception.retry_after, 7)

    async def test_catalog_failure_is_a_read_failure_not_a_mutation_attempt(self):
        self.catalog = {1}
        with self.assertRaisesRegex(ValueError, "unknown forum topic"):
            await self.actions.react(PEER, 73, "👍", topic_id=TOPIC)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
