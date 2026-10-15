"""Contract tests for forum topics, with simulated Telethon TL fields only."""

from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from userbot.topic import classify_message, reply_target


@dataclass(frozen=True)
class MessageReplyHeader:
    forum_topic: bool | None = None
    reply_to_msg_id: int | None = None
    reply_to_top_id: int | None = None


class MessageActionTopicCreate:
    CONSTRUCTOR_ID = 0x0D999256


def message(message_id=101, header=None, action=None):
    return SimpleNamespace(id=message_id, reply_to=header, action=action)


CATALOG = {1, 40, 88}


class TopicClassificationTests(unittest.TestCase):
    def test_general_without_header_and_general_reply(self):
        self.assertIsNone(classify_message(message(), CATALOG))
        with self.assertRaisesRegex(ValueError, "refresh catalog"):
            classify_message(message(header=MessageReplyHeader(forum_topic=True, reply_to_msg_id=777)), CATALOG)

    def test_topic_root_creation_action_uses_its_own_id(self):
        self.assertEqual(classify_message(message(message_id=40, action=MessageActionTopicCreate()), CATALOG), 40)
        with self.assertRaisesRegex(ValueError, "unknown topic ID"):
            classify_message(message(message_id=999, action=MessageActionTopicCreate()), CATALOG)

    def test_top_id_is_authoritative_even_for_nested_reply(self):
        self.assertEqual(
            classify_message(
                message(header=MessageReplyHeader(forum_topic=True, reply_to_msg_id=777, reply_to_top_id=40)),
                CATALOG,
            ),
            40,
        )
        # The explicit top is used, not a known reply root in another topic.
        self.assertEqual(
            classify_message(message(header=MessageReplyHeader(reply_to_msg_id=88, reply_to_top_id=40)), CATALOG), 40
        )

    def test_root_reply_without_top_only_when_forum_topic_flag_is_set(self):
        self.assertEqual(
            classify_message(message(header=MessageReplyHeader(forum_topic=True, reply_to_msg_id=88)), CATALOG), 88
        )
        self.assertIsNone(classify_message(message(header=MessageReplyHeader(reply_to_msg_id=88)), CATALOG))
        with self.assertRaisesRegex(ValueError, "refresh catalog"):
            classify_message(message(header=MessageReplyHeader(forum_topic=True, reply_to_msg_id=999)), CATALOG)

    def test_unknown_explicit_topic_and_malformed_top_never_fall_back_to_general(self):
        for top in (999, 1, 0, True, "40"):
            with self.subTest(top=top), self.assertRaises(ValueError):
                classify_message(message(header=MessageReplyHeader(forum_topic=True, reply_to_top_id=top)), CATALOG)

    def test_catalog_requires_validated_general_and_int32_ids(self):
        for bad in ({40}, {1, 0}, {True, 40}, {1, 2**31}, [1, 40], None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                classify_message(message(), bad)
        self.assertEqual(classify_message(message(header=MessageReplyHeader(reply_to_top_id=40)), frozenset(CATALOG)), 40)


class OutgoingTargetTests(unittest.TestCase):
    def test_general_direct_send_and_reply_are_unthreaded(self):
        self.assertEqual(reply_target(None, None, CATALOG), {"reply_to_msg_id": None, "top_msg_id": None})
        self.assertEqual(reply_target(1, 777, CATALOG), {"reply_to_msg_id": 777, "top_msg_id": None})
        self.assertEqual(reply_target(None, 777, CATALOG), {"reply_to_msg_id": 777, "top_msg_id": None})

    def test_non_general_direct_and_nested_reply(self):
        self.assertEqual(reply_target(40, None, CATALOG), {"reply_to_msg_id": 40, "top_msg_id": None})
        self.assertEqual(reply_target(40, 40, CATALOG), {"reply_to_msg_id": 40, "top_msg_id": None})
        self.assertEqual(reply_target(40, 777, CATALOG), {"reply_to_msg_id": 777, "top_msg_id": 40})

    def test_outbound_catalog_and_ids_fail_closed(self):
        for bad_topic in (999, 0, True, "40", 2**31):
            with self.subTest(topic=bad_topic), self.assertRaises(ValueError):
                reply_target(bad_topic, None, CATALOG)
        for bad_reply in (0, True, "777", 2**31):
            with self.subTest(reply=bad_reply), self.assertRaises(ValueError):
                reply_target(40, bad_reply, CATALOG)
        with self.assertRaises(ValueError):
            reply_target(None, None, {40})


if __name__ == "__main__":
    unittest.main()
