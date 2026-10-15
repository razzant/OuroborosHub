"""One second-bot polling loop: capture → private media → exact Presence → outbox.

No update is acknowledged to Telegram before its private row is durable. Capture
is independent of Presence enrollment; neither a private receipt nor a pending
Host turn grants a public reply. All network sends run through BotTransport's
current host-owned token-separation check.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import binding_for, load
from .host import HostClient
from .media import LegacyAudioReceipt, MediaStore, describe
from .store import Store
from .transport import BotTransport, TransportUnavailable


def source_message(update: dict) -> tuple[dict, str] | None:
    if type(update) is not dict:
        return None
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        if type(update.get(key)) is dict:
            return update[key], key
    return None


def identity(message: dict, *, bot_id: int) -> tuple[int, int | None, int, int] | None:
    """Prove chat/topic/message/actor from Bot API fields, never guess General."""
    chat = message.get("chat")
    if type(chat) is not dict or type(chat.get("id")) is not int:
        return None
    cid, mid = chat["id"], message.get("message_id")
    if cid == 0 or type(mid) is not int or mid <= 0:
        return None
    thread = message.get("message_thread_id")
    forum = chat.get("is_forum") is True
    if forum and (type(thread) is not int or thread <= 0):
        return None
    if not forum and thread is not None:
        return None
    topic = None if thread in (None, 1) else thread
    actor_obj = message.get("from") or message.get("sender_chat")
    actor = actor_obj.get("id") if type(actor_obj) is dict else None
    if type(actor) is not int or actor == 0:
        return None
    if actor == bot_id:
        return None  # never answer our own outgoing event
    return cid, topic, mid, actor


def _event(account: int, chat: int, topic: int | None, mid: int, actor: int,
           update_id: int, kind: str, text: str, media: dict | None) -> dict:
    tid = str(topic) if topic is not None else ""
    source = f"telegram:{account}:{chat}:{tid or '0'}:{mid}:{kind}:{update_id}"
    return {"source_event_id": source, "provider": "telegram", "account_id": str(account),
            "conversation_id": str(chat), "thread_id": tid,
            "conversation_key": f"telegram:{account}:{chat}:{tid or '0'}",
            "actor": {"platform_actor_id": str(actor)}, "conversation": {},
            "message": {"message_id": str(mid), "event_kind": kind,
                        "media": ({"kind": media["kind"], "status": media["status"]} if media else None)},
            "text": text}


def _media_text(message: dict, receipt: dict | None, transcript: dict) -> str:
    text = message.get("text") or message.get("caption") or ""
    if not isinstance(text, str):
        text = ""
    if receipt:
        text += ("\n" if text else "") + (
            f"[Telegram {receipt['kind']} media: {receipt['status']}; message {message['message_id']}]"
        )
        if receipt["kind"] in {"voice", "audio"}:
            if transcript.get("status") == "ready":
                text += "\n[Local audio transcription: " + transcript["text"] + "]"
            elif transcript.get("status") == "no_speech":
                text += "\n[No speech detected in local audio; source file retained.]"
            else:
                text += "\n[Local audio transcription unavailable: " + transcript.get("status", "not_run") + "]"
    return text


def process_one(row: dict, *, account: int, config: dict, store: Store,
                media: MediaStore, bot: BotTransport, host: HostClient) -> tuple[bool, int | None]:
    """Process one saved update; bool means it still waits on Host/network."""
    uid = row["update_id"]
    update = json.loads(row["body"])
    selected = source_message(update)
    if selected is None:
        store.settle(uid, held=True)  # unsupported update remains private
        return False, None
    message, kind = selected
    if describe(message) is None and any(key in message for key in (
            "sticker", "animation", "paid_media", "story", "video_note", "photo",
            "voice", "audio", "video", "document")):
        # The raw update still carries the provider identity and descriptor;
        # never ask a model to answer invisible media as an empty text turn.
        store.settle(uid, held=True)
        return False, (message.get("chat") or {}).get("id")
    chat_info = message.get("chat") if type(message.get("chat")) is dict else {}
    chat_id = chat_info.get("id") if type(chat_info.get("id")) is int else None
    descriptor = describe(message)
    receipt = media.capture(uid, descriptor, config, bot) if descriptor else None
    route = identity(message, bot_id=account)
    if route is None:
        store.settle(uid, held=True)  # unknown actor/topic cannot become General
        return False, chat_id
    chat, topic, mid, actor = route
    binding = binding_for(config, chat, topic)
    if binding is None or row["binding_id"] != binding:
        store.settle(uid, held=True)
        return False, chat
    store.freeze_binding(uid, binding)
    try:
        ready = media.ready_file(uid) if receipt and receipt["status"] == "ready" else None
    except LegacyAudioReceipt:
        # Keep the old file and provider update for explicit owner repair;
        # neither retry the same poisoned turn nor crash the whole companion.
        store.freeze_transcript(uid, {"status": "legacy_audio_suffix_ambiguous", "text": ""})
        store.settle(uid, held=True)
        return False, chat
    transcript = json.loads(row["transcript"]) if row["transcript"] else {}
    if (not row["work_ref"] and transcript.get("status") == "audio_unsupported"
            and receipt and receipt["kind"] in {"voice", "audio"}
            and ";" in receipt.get("mime", "")
            and receipt["mime"].split(";", 1)[0].strip().lower() in {"audio/ogg", "audio/webm"}):
        # A pre-fix decision may precede an unconfirmed Host POST. Re-running
        # transcription would change that event's text under the same source id.
        store.hold_frozen_transcript(uid, "legacy_parameterized_audio_unresolved")
        return False, chat
    if ready and receipt["kind"] in {"voice", "audio"} and not transcript:
        # Media capture does not grant model access. Its local/Host processing
        # may have waited through an owner edit since this pass's config read.
        if binding_for(load(media.root.parent), chat, topic) != binding:
            store.settle(uid, held=True)
            return False, chat
        text, state = host.transcribe(binding, account, chat, topic,
                                      ready[0].read_bytes(), receipt["mime"])
        transcript = store.freeze_transcript(uid, {"status": state, "text": text})
    body = _media_text(message, receipt, transcript)
    event = _event(account, chat, topic, mid, actor, uid, kind, body, receipt)
    work = row["work_ref"]
    # The transcriber may have blocked for minutes. A captured binding is an
    # identity, not continuing permission after the owner removes it.
    if binding_for(load(media.root.parent), chat, topic) != binding:
        store.settle(uid, held=True)
        return False, chat
    try:
        response = host.work(binding, work) if work else (
            host.turn(binding, event, [str(ready[0])] if ready else []))
    except TransportUnavailable:
        return True, chat
    if response.get("ok") is not True:
        if response.get("disposition") in {"blocked", "rejected"}:
            store.settle(uid, held=True)
            return False, chat
        return True, chat
    returned_work = response.get("work_ref")
    if isinstance(returned_work, str) and returned_work:
        store.set_work(uid, binding, returned_work)
    if response.get("status") == "pending":
        return True, chat
    if response.get("status") in {"failed", "cancelled"}:
        store.settle(uid, held=True)
        return False, chat
    if response.get("status") != "completed":
        return True, chat
    outcome = response.get("outcome")
    reply = response.get("text")
    if (outcome in {"deferred", "message"} and isinstance(reply, str) and reply.strip()
            and len(reply.encode("utf-8")) > 4096):
        store.hold_reply_too_long(uid, reply)
        return False, chat
    if outcome == "deferred":
        if not (returned_work or work):
            store.settle(uid, held=True)
            return False, chat
        if isinstance(response.get("text"), str) and response["text"].strip():
            store.queue_reply(event["source_event_id"] + ":interim", chat, topic, mid,
                              binding, response["text"])
        return True, chat
    if outcome == "message" and isinstance(response.get("text"), str) and response["text"].strip():
        store.queue_reply(event["source_event_id"], chat, topic, mid, binding, response["text"])
    elif outcome not in {"message", "silent", "tool_delivered"}:
        return True, chat
    store.settle(uid)
    return False, chat


def send_one(store: Store, config: dict, bot: BotTransport, host: HostClient,
             account: int, state_dir: Path) -> bool:
    job = store.claim_reply()  # durable attempted mark BEFORE the first network call
    if job is None:
        return False
    source = job["source_id"]
    if binding_for(config, job["chat_id"], job["topic_id"]) != job["binding_id"]:
        store.unknown(source)  # no send, but never re-authorize old speech
        return True
    payload = {"chat_id": job["chat_id"], "text": job["text"],
               "reply_parameters": {"message_id": job["reply_id"]}}
    if job["topic_id"] is not None:
        payload["message_thread_id"] = job["topic_id"]
    try:
        result = bot.api("sendMessage", payload, pre_send=lambda: (
            binding_for(load(state_dir), job["chat_id"], job["topic_id"]) == job["binding_id"]))
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TransportUnavailable("Telegram send receipt unavailable")
        thread = result.get("message_thread_id")
        if job["topic_id"] is not None and thread != job["topic_id"]:
            raise TransportUnavailable("Telegram replied in another topic")
        store.sent(source, result["message_id"])
    except TransportUnavailable:
        store.unknown(source)
    return True


def report_sent(store: Store, host: HostClient, account: int) -> None:
    for job in store.pending_reports(limit=3):
        if host.delivered(binding_id=job["binding_id"], source_id=job["source_id"],
                          account=account, chat=job["chat_id"], topic=job["topic_id"],
                          text=job["text"], message_id=job["remote_id"]):
            store.reported(job["source_id"])


def run(state_dir: Path, *, bot: BotTransport, host: HostClient, max_polls: int | None = None) -> None:
    """The installed companion calls this without max_polls; tests inject fakes."""
    host.identity()  # positive Host authentication before Telegram
    me = bot.api("getMe", {})
    if (type(me) is not dict or me.get("is_bot") is not True or
            type(me.get("id")) is not int or me["id"] <= 0):
        raise TransportUnavailable("authenticated second bot identity unavailable")
    account = me["id"]
    state_dir = Path(state_dir)
    with Store(state_dir / "transport.sqlite3", recover_attempted=True) as store:
        store.bind_account(account)
        media = MediaStore(state_dir)
        polls = 0
        while max_polls is None or polls < max_polls:
            polls += 1
            before_poll = load(state_dir)
            updates = bot.api("getUpdates", {"offset": store.offset(), "timeout": 25,
                "limit": 100, "allowed_updates": ["message", "edited_message", "channel_post", "edited_channel_post"]},
                timeout=35)
            if type(updates) is not list or len(updates) > 100:
                raise TransportUnavailable("Bot API updates unavailable")
            if any(type(item) is not dict or type(item.get("update_id")) is not int for item in updates):
                raise TransportUnavailable("Bot API update identity unavailable")
            for update in sorted(updates, key=lambda item: item["update_id"]):
                config = load(state_dir)  # never use a pre-longpoll mapping
                selected = source_message(update)
                message = selected[0] if selected else {}
                route = identity(message, bot_id=account) if selected else None
                selected_binding = binding_for(config, route[0], route[1]) if route else None
                stable_binding = (selected_binding if route and
                                  selected_binding == binding_for(before_poll, route[0], route[1])
                                  else None)
                media_keys = {"photo", "voice", "audio", "video", "video_note", "document",
                              "sticker", "animation", "paid_media", "story"}
                if (describe(message) is not None or media_keys.intersection(message)
                        or selected_binding):
                    store.capture(update, binding_id=stable_binding)
                else:
                    store.skip_unbound_text(update)
            blocked_chats: set[int] = set()
            for row in store.pending(limit=100):
                update = json.loads(row["body"])
                selected = source_message(update)
                chat = (selected[0].get("chat") or {}).get("id") if selected else None
                if chat in blocked_chats:
                    continue
                try:
                    config = load(state_dir)  # recheck after capture, before Host admission
                    pending, chat = process_one(row, account=account, config=config, store=store,
                                                media=media, bot=bot, host=host)
                except TransportUnavailable:
                    # Media was not captured. Keep the raw row and its exact
                    # provider id pending; another chat may still progress.
                    pending = True
                if pending and type(chat) is int:
                    blocked_chats.add(chat)
            send_one(store, load(state_dir), bot, host, account, state_dir)  # current pre-send authority
            report_sent(store, host, account)
