from telegram_bot.custody import CustodyStore
from telegram_bot.events import parse_telegram_update


def test_parses_exact_actor_chat_topic_message_and_media_provenance():
    event = parse_telegram_update(
        {
            "update_id": 77,
            "message": {
                "message_id": 9,
                "message_thread_id": 42,
                "is_topic_message": True,
                "date": 1_700_000_000,
                "caption": "See both files",
                "reply_to_message": {"message_id": 8},
                "from": {
                    "id": 123,
                    "is_bot": False,
                    "username": "reader",
                    "first_name": "R",
                    "last_name": "D",
                    "language_code": "en",
                },
                "chat": {"id": -456, "type": "supergroup", "title": "Community"},
                "photo": [
                    {"file_id": "small", "file_unique_id": "p1", "file_size": 10},
                    {"file_id": "large", "file_unique_id": "p2", "file_size": 20},
                ],
                "document": {
                    "file_id": "doc",
                    "file_unique_id": "d1",
                    "file_name": "notes.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 30,
                },
            },
        },
        bot_account_id="900",
    )

    assert event is not None
    assert event.source_event_id == "telegram:900:77"
    assert event.account_id == "900"
    assert event.conversation_id == "-456"
    assert event.thread_id == "42"
    assert event.conversation_key == "telegram:900:-456:42"
    assert event.actor["platform_actor_id"] == "123"
    assert event.conversation["topic_id"] == 42
    assert event.message["message_id"] == 9
    assert event.message["sent_at_epoch"] == 1_700_000_000
    assert event.message["sent_at"] == "2023-11-14T22:13:20+00:00" and "edited_at" not in event.message
    assert event.message["reply_to_message_id"] == 8
    assert [item["file_id"] for item in event.message["attachments"]] == [
        "large",
        "doc",
    ]
    assert set(event.to_dict()) == {
        "source_event_id",
        "provider",
        "account_id",
        "conversation_id",
        "thread_id",
        "conversation_key",
        "actor",
        "conversation",
        "message",
        "text",
    }
    assert event.to_dict()["text"] == "See both files"
    edited = parse_telegram_update({"update_id": 78, "edited_message": {
        "message_id": 9, "date": 1_700_000_000, "edit_date": 1_700_000_060, "text": "Corrected",
        "from": {"id": 123, "is_bot": False}, "chat": {"id": -456, "type": "supergroup"}}}, bot_account_id="900")
    assert (edited.message["sent_at"], edited.message["edited_at"]) == (
        "2023-11-14T22:13:20+00:00", "2023-11-14T22:14:20+00:00")


def test_ignores_bot_edits_service_and_empty_messages():
    base = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "from": {"id": 3, "is_bot": False},
            "chat": {"id": 4, "type": "private"},
        },
    }
    assert parse_telegram_update(base, bot_account_id="9") is None
    bot = {
        **base,
        "message": {**base["message"], "text": "x", "from": {"id": 3, "is_bot": True}},
    }
    assert parse_telegram_update(bot, bot_account_id="9") is None
    edited = {"update_id": 1, "edited_message": {**base["message"], "text": "x"}}
    parsed_edit = parse_telegram_update(edited, bot_account_id="9")
    assert parsed_edit is not None
    assert parsed_edit.message["event_kind"] == "edited_message"
    reaction = {
        "update_id": 2,
        "message_reaction": {
            "user": {"id": 3, "is_bot": False},
            "chat": {"id": 4, "type": "private"},
            "message_id": 2,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "👍"}],
        },
    }
    parsed_reaction = parse_telegram_update(reaction, bot_account_id="9")
    assert parsed_reaction is not None
    assert parsed_reaction.message["event_kind"] == "message_reaction"
    assert parsed_reaction.text == ""


def test_configured_group_is_context_not_owner_authority():
    update = {
        "update_id": 8,
        "message": {
            "message_id": 12,
            "text": "/restart is conversation text",
            "from": {"id": 30, "is_bot": False},
            "chat": {"id": -42, "type": "group"},
        },
    }
    event = parse_telegram_update(
        update, bot_account_id="9", management_group_id="-42"
    ).to_dict()
    assert event["conversation"]["configured_room"] == "management_group"
    assert event["text"] == "/restart is conversation text"
    assert "owner" not in event and "role" not in event["actor"]
    assert (
        "configured_room"
        not in parse_telegram_update(
            update, bot_account_id="9", management_group_id="-43"
        ).conversation
    )
    update["message"]["from"]["is_bot"] = True
    assert (
        parse_telegram_update(update, bot_account_id="9", management_group_id="-42")
        is None
    )


def _group_message(update_id, *, text="hi", thread=None, chat_extra=None, **message_extra):
    message = {
        "message_id": update_id,
        "text": text,
        "from": {"id": 30, "is_bot": False},
        "chat": {"id": -100, "type": "supergroup", "title": "Room", **(chat_extra or {})},
        **message_extra,
    }
    if thread is not None:
        message["message_thread_id"] = thread
    return parse_telegram_update(
        {"update_id": update_id, "message": message}, bot_account_id="9"
    )


def test_reply_chain_in_ordinary_group_stays_in_the_chat_conversation():
    top_level = _group_message(1)
    reply = _group_message(2, thread=1, reply_to_message={"message_id": 1})
    assert top_level is not None and reply is not None
    # Telegram names a reply chain with message_thread_id outside forums; it is
    # not a topic, so the reply joins the chat's single conversation key.
    assert reply.conversation_key == top_level.conversation_key == "telegram:9:-100:0"
    assert reply.thread_id == ""
    assert reply.conversation["topic_id"] is None
    assert reply.message["reply_chain_id"] == 1
    assert reply.message["reply_to_message_id"] == 1
    assert "reply_chain_id" not in top_level.message


def test_only_a_topic_message_keeps_its_own_conversation_key():
    topic = _group_message(3, thread=42, is_topic_message=True, chat_extra={"is_forum": True})
    assert topic is not None
    assert topic.conversation_key == "telegram:9:-100:42"
    assert topic.thread_id == "42"
    assert topic.conversation["topic_id"] == 42
    assert "reply_chain_id" not in topic.message
    general = _group_message(5, chat_extra={"is_forum": True})
    assert general is not None and general.conversation_key == "telegram:9:-100:0"


def test_forum_general_reply_is_a_reply_chain_not_a_topic(tmp_path):
    # A reply in a forum's General topic carries message_thread_id but no
    # is_topic_message; the forum flag alone must not open a topic.
    reply = _group_message(
        6, thread=5, chat_extra={"is_forum": True}, reply_to_message={"message_id": 5}
    )
    assert reply is not None
    assert reply.conversation_key == "telegram:9:-100:0"
    assert reply.thread_id == ""
    assert reply.conversation["topic_id"] is None
    assert reply.message["reply_chain_id"] == 5
    store = CustodyStore(tmp_path / "custody.sqlite3")
    store.commit_update(6, reply)
    lease = store.claim_inbox()
    store.record_submission(
        lease.event_id, binding_id="b" * 32, turn_ref="turn-6",
        outcome="message", text="noted", work_ref="",
    )
    outbox = store.claim_outbox()
    assert outbox is not None and outbox.payload["chat_id"] == "-100"
    assert "topic_id" not in outbox.payload
    assert outbox.payload["reply_to_message_id"] == 6


def test_edited_topic_message_keeps_the_topic_key():
    edited = parse_telegram_update(
        {
            "update_id": 11,
            "edited_message": {
                "message_id": 10,
                "message_thread_id": 42,
                "is_topic_message": True,
                "text": "fixed typo",
                "from": {"id": 30, "is_bot": False},
                "chat": {"id": -100, "type": "supergroup", "is_forum": True},
            },
        },
        bot_account_id="9",
    )
    assert edited is not None
    assert edited.message["event_kind"] == "edited_message"
    assert edited.conversation_key == "telegram:9:-100:42"
    assert edited.thread_id == "42"


def test_private_chat_and_reactions_use_the_chat_conversation():
    private = parse_telegram_update(
        {
            "update_id": 6,
            "message": {
                "message_id": 6,
                "text": "hello",
                "message_thread_id": 5,
                "from": {"id": 3, "is_bot": False},
                "chat": {"id": 3, "type": "private"},
            },
        },
        bot_account_id="9",
    )
    assert private is not None
    assert private.conversation_key == "telegram:9:3:0"
    assert private.message["reply_chain_id"] == 5
    private_topic = parse_telegram_update(
        {
            "update_id": 12,
            "message": {
                "message_id": 12,
                "text": "in a private topic",
                "message_thread_id": 5,
                "is_topic_message": True,
                "from": {"id": 3, "is_bot": False},
                "chat": {"id": 3, "type": "private"},
            },
        },
        bot_account_id="9",
    )
    assert private_topic is not None
    assert private_topic.conversation_key == "telegram:9:3:5"
    assert "reply_chain_id" not in private_topic.message
    reaction = parse_telegram_update(
        {
            "update_id": 7,
            "message_reaction": {
                "user": {"id": 30, "is_bot": False},
                "chat": {"id": -100, "type": "supergroup"},
                "message_id": 2,
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            },
        },
        bot_account_id="9",
    )
    assert reaction is not None
    assert reaction.conversation_key == "telegram:9:-100:0"
    assert reaction.conversation["topic_id"] is None


def test_album_member_carries_media_group_id_only_when_telegram_sends_it():
    album = _group_message(8, text="", caption="one of two", media_group_id="1357")
    single = _group_message(9)
    assert album is not None and single is not None
    assert album.message["media_group_id"] == "1357"
    assert "media_group_id" not in single.message
