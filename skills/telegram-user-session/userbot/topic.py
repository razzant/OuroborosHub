"""Pure, fail-closed forum-topic routing for one *known forum peer*.

The caller supplies a refreshed topic catalog from Telegram for the same peer;
this module never guesses a topic by traversing reply chains or fetching messages.
``None`` is the application spelling of General (Telegram topic id 1).

Telegram forum contract: https://core.telegram.org/api/forum
TL reply header: https://tl.telethon.dev/constructors/message_reply_header.html
"""

from __future__ import annotations


GENERAL_TOPIC_ID = 1
_MAX_MESSAGE_ID = 2**31 - 1
_TOPIC_CREATE_CONSTRUCTOR_ID = 0x0D999256


def _message_id(value: object, name: str) -> int:
    # bool is an int subclass, but never a Telegram message/topic identifier.
    if type(value) is not int or not 1 <= value <= _MAX_MESSAGE_ID:
        raise ValueError(f"{name} must be a positive Telegram int32 ID")
    return value


def _catalog(known_topics: set[int] | frozenset[int]) -> set[int] | frozenset[int]:
    if not isinstance(known_topics, (set, frozenset)):
        raise ValueError("known_topics must be a set of topic IDs for this forum peer")
    if GENERAL_TOPIC_ID not in known_topics:
        raise ValueError("known_topics must include General (topic id 1)")
    for topic_id in known_topics:
        _message_id(topic_id, "catalog topic ID")
    return known_topics


def _known(topic_id: object, known_topics: set[int] | frozenset[int]) -> int:
    topic_id = _message_id(topic_id, "topic ID")
    if topic_id not in known_topics:
        raise ValueError(f"unknown topic ID {topic_id}; refresh this forum's catalog")
    return topic_id


def classify_message(message: object, known_topics: set[int] | frozenset[int]) -> int | None:
    """Classify a Telegram message from the catalog's forum peer.

    With ``forum_topic`` true, ``reply_to_top_id`` identifies non-General topics;
    false identifies General even when an ordinary message thread has a top. The topic
    creation service message has no required top header: its own ID *is* the
    root. A reply directly to a known topic root is another explicit signal,
    but only when Telegram marks the header as ``forum_topic``. Other reply IDs
    are ordinary message IDs, NOT candidate topic IDs. An ambiguous forum reply
    with no top and an unknown direct target raises for a caller-side catalog
    refresh and optional fetched-target proof; it is not silently General.
    Peer/forum validation and
    catalog refresh on creation/deletion remain the caller's responsibility.
    """
    catalog = _catalog(known_topics)
    if message is None:
        raise ValueError("message is required")
    action = getattr(message, "action", None)
    if getattr(action, "CONSTRUCTOR_ID", None) == _TOPIC_CREATE_CONSTRUCTOR_ID:
        topic_id = _known(getattr(message, "id", None), catalog)
        return None if topic_id == GENERAL_TOPIC_ID else topic_id

    header = getattr(message, "reply_to", None)
    if header is None:
        return None
    # Telegram's top ID names any message thread, not necessarily a forum
    # topic. The explicit false flag means General even for a threaded reply.
    # https://core.telegram.org/constructor/messageReplyHeader (checked 2026-10-24)
    if getattr(header, "forum_topic", None) is False:
        return None
    top = getattr(header, "reply_to_top_id", None)
    if top is not None:
        topic_id = _known(top, catalog)
        if topic_id == GENERAL_TOPIC_ID:
            raise ValueError("General must not carry reply_to_top_id")
        return topic_id

    reply_id = getattr(header, "reply_to_msg_id", None)
    if getattr(header, "forum_topic", None) is True and reply_id is not None:
        # A reply to the service/root message has an explicit topic identity;
        # an arbitrary reply_to_msg_id never does.
        _message_id(reply_id, "reply_to_msg_id")
        if reply_id in catalog and reply_id != GENERAL_TOPIC_ID:
            return reply_id
        # A direct reply to a newly created root looks exactly like a General
        # reply while the cached catalog is stale. Let the caller refresh once;
        # if still unknown, hold rather than borrow General's Presence binding.
        raise ValueError("forum reply without known topic root; refresh catalog")
    return None


def reply_target(
    topic_id: int | None,
    reply_id: int | None,
    known_topics: set[int] | frozenset[int],
) -> dict[str, int | None]:
    """Specify InputReplyToMessage fields; ``None`` values mean omit them.

    Caller constructs the raw Telethon InputReplyToMessage only if
    ``reply_to_msg_id`` is present. A General reply uses that ID alone; a
    non-General direct send replies to the topic root; a reply to a message
    inside that topic adds ``top_msg_id`` to preserve routing even if the
    replied-to message gets deleted before Telegram completes the send.
    This does not prove ``reply_id`` belongs to ``topic_id``: the caller must
    bind it to the accepted inbound message for this peer/topic.
    """
    catalog = _catalog(known_topics)
    canonical_topic = GENERAL_TOPIC_ID if topic_id is None else _known(topic_id, catalog)
    if reply_id is not None:
        reply_id = _message_id(reply_id, "reply_id")
    if canonical_topic == GENERAL_TOPIC_ID:
        return {"reply_to_msg_id": reply_id, "top_msg_id": None}
    target_id = canonical_topic if reply_id is None else reply_id
    return {
        "reply_to_msg_id": target_id,
        "top_msg_id": canonical_topic if target_id != canonical_topic else None,
    }
