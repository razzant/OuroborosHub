"""Raw Telegram Bot API updates, shaped as ``getUpdates`` returns them (synthetic people, titles and ids)."""
from __future__ import annotations

import itertools

_date = itertools.count(1_790_000_000)
_message = itertools.count(100)

PERSON = {"id": 7001, "is_bot": False, "first_name": "Mira", "username": "mira_synthetic"}
OTHER = {"id": 7002, "is_bot": False, "first_name": "Tom", "username": "tom_synthetic"}


def _chat(chat_id: int, title: str | None, kind: str) -> dict:
    chat = {"id": chat_id, "type": kind}
    if title is not None:
        chat["title"] = title
    if kind == "supergroup" and title and chat_id < -1_000_000_000_000:
        chat["is_forum"] = False
    return chat


def group_message(chat_id: int, title: str, text: str = "hello", *, kind: str = "supergroup", sender: dict = PERSON,
                  topic: int | None = None) -> dict:
    message = {"message_id": next(_message), "date": next(_date), "chat": _chat(chat_id, title, kind),
               "from": sender, "text": text}
    if topic is not None:
        message.update(message_thread_id=topic, is_topic_message=True)
        message["chat"]["is_forum"] = True
    return {"message": message}


def private_message(person: dict = OTHER, text: str = "hi") -> dict:
    return {"message": {"message_id": next(_message), "date": next(_date),
                        "chat": {"id": person["id"], "type": "private", "first_name": person["first_name"]},
                        "from": person, "text": text}}


def channel_post(chat_id: int, title: str, text: str = "announcement") -> dict:
    return {"channel_post": {"message_id": next(_message), "date": next(_date), "chat": _chat(chat_id, title, "channel"),
                             "text": text}}


def membership(chat_id: int, title: str, bot: dict, *, was: str, now: str, kind: str = "supergroup") -> dict:
    """``my_chat_member``: the bot's own membership changed (metadata only: no message, no sender text)."""
    def member(status):
        return {"user": bot, "status": status}
    return {"my_chat_member": {"chat": _chat(chat_id, title, kind), "from": OTHER, "date": next(_date),
                               "old_chat_member": member(was), "new_chat_member": member(now)}}


def service(chat_id: int, title: str, *, kind: str = "supergroup", **fact) -> dict:
    """A service message: a join, a leave or a migration, with no text, caption or media."""
    return {"message": {"message_id": next(_message), "date": next(_date), "chat": _chat(chat_id, title, kind),
                        "from": OTHER, **fact}}
