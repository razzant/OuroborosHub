"""Voice messages and audio files are ordinary attachments: original bytes, no words."""
import asyncio
import pathlib

import pytest

from telegram_bot.events import BOT_API_DOWNLOAD_LIMIT, parse_telegram_update
from telegram_bot.runtime import TelegramTransportRuntime

from test_runtime import AcceptingSubmitter, Logger, RuntimeClient


def _update(update_id=7, *, chat_type="private", **message):
    chat = {"id": 4 if chat_type == "private" else -100, "type": chat_type, "title": "" if chat_type == "private" else "Room"}
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": 1_700_000_000, "from": {"id": 3, "is_bot": False, "first_name": "Ann"},
        "chat": chat, **message}}


VOICE = {"file_id": "v-id", "file_unique_id": "v-uniq", "duration": 4, "mime_type": "audio/ogg", "file_size": 9000}


def test_voice_only_message_is_delivered_as_an_attachment_without_text():
    event = parse_telegram_update(_update(voice=VOICE), bot_account_id="9")
    assert event is not None  # 0.10.0 dropped this message: no text, no supported attachment
    assert event.text == ""
    assert event.message["attachments"] == [{
        "kind": "voice", "file_id": "v-id", "file_unique_id": "v-uniq", "file_name": "voice-v-uniq.ogg",
        "mime_type": "audio/ogg", "file_size": 9000, "duration_sec": 4}]


def test_voice_without_declared_type_is_ogg_and_wakes_like_an_empty_message():
    voice = {"file_id": "v", "file_unique_id": "u", "duration": 2}
    event = parse_telegram_update(_update(voice=voice), bot_account_id="9", places=True, bot_username="presence_bot",
                                  words=("hello",))
    attachment = event.message["attachments"][0]
    assert (attachment["file_name"], attachment["mime_type"]) == ("voice-u.ogg", "audio/ogg")
    assert event.facts == ["any_message", "direct_message"]  # no mention or word signal from a recording
    group = parse_telegram_update(_update(chat_type="supergroup", voice=voice), bot_account_id="9", places=True,
                                  bot_username="presence_bot", words=("hello",))
    assert group.facts == ["any_message"]


def test_caption_stays_the_only_text_and_reply_to_me_is_unchanged():
    update = _update(chat_type="supergroup", voice=VOICE, caption="listen", reply_to_message={
        "message_id": 1, "from": {"id": 9, "is_bot": True}, "chat": {"id": -100, "type": "supergroup"}})
    event = parse_telegram_update(update, bot_account_id="9", places=True)
    assert event.text == "listen"
    assert event.facts == ["any_message", "reply_to_me"]


@pytest.mark.parametrize("given,mime,expected", [
    ("song.mp3", "audio/mpeg", "song.mp3"),
    ("Song.MP3", "audio/mpeg", "Song.MP3"),
    ("recording", "audio/mpeg", "recording.mp3"),
    ("memo.m4a", "audio/x-m4a", "memo.m4a"),
    ("memo.mp4", "audio/mp4", "memo.mp4.m4a"),  # a generic MP4 brand is audio only as .m4a
    ("talk.opus", "audio/ogg", "talk.opus"),
    ("tune.flac", "audio/flac", "tune.flac"),
    ("", "audio/mpeg", "audio-a-uniq.mp3"),
    ("blob", "application/x-unknown", "blob"),  # unknown type: no suffix is invented
])
def test_audio_file_keeps_its_name_with_a_suffix_matching_its_type(given, mime, expected):
    audio = {"file_id": "a-id", "file_unique_id": "a-uniq", "duration": 200, "mime_type": mime,
             "file_size": 5_000_000, "title": "Talk", "performer": "Ann", **({"file_name": given} if given else {})}
    event = parse_telegram_update(_update(audio=audio), bot_account_id="9")
    attachment = event.message["attachments"][0]
    assert (attachment["kind"], attachment["file_name"], attachment["mime_type"]) == ("audio", expected, mime)
    assert attachment["duration_sec"] == 200 and "status" not in attachment
    assert event.text == ""  # a title is metadata, never offered as words


def test_photo_and_document_facts_keep_their_exact_earlier_shape():
    event = parse_telegram_update(_update(
        photo=[{"file_id": "p", "file_unique_id": "pu", "file_size": 3}],
        document={"file_id": "d", "file_unique_id": "du", "file_name": "a.pdf", "mime_type": "application/pdf", "file_size": 5},
    ), bot_account_id="9")
    assert [set(item) for item in event.message["attachments"]] == [
        {"kind", "file_id", "file_unique_id", "file_name", "mime_type", "file_size"}] * 2


def test_replied_to_recording_is_described_not_staged():
    update = _update(text="what did you say?", reply_to_message={
        "message_id": 1, "from": {"id": 30}, "chat": {"id": 4, "type": "private"}, "voice": VOICE})
    event = parse_telegram_update(update, bot_account_id="9")
    assert event.message["attachments"] == []
    assert event.message["reply_to_message"]["attachments"][0]["kind"] == "voice"


def _runtime(tmp_path):
    submitter, clients = AcceptingSubmitter(), []

    def factory(token):
        clients.append(RuntimeClient(token))
        return clients[-1]
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                       submitter=submitter, client_factory=factory)
    return runtime, submitter, clients


@pytest.mark.parametrize("kind,mime,name,expected", [
    ("voice", "audio/ogg", "", "voice-v-uniq.ogg"),
    ("audio", "audio/mpeg", "a" * 180 + ".mp3", "a" * 156 + ".mp3"),
    ("audio", "audio/x-m4a", "a" * 180 + ".m4a", "a" * 156 + ".m4a"),
])
def test_voice_is_staged_under_its_typed_name_and_submitted(tmp_path, kind, mime, name, expected):
    runtime, submitter, clients = _runtime(tmp_path)
    sound = {**VOICE, "mime_type": mime, "file_name": name}
    runtime.store.commit_update(7, parse_telegram_update(_update(**{kind: sound}), bot_account_id="9"))
    assert asyncio.run(runtime.process_one_inbox())
    event, staged = submitter.calls[0]
    assert [path.name for path in staged] == ["0-" + expected]
    assert clients[0].downloads == [("v-id", staged[0])]
    assert event["text"] == "" and event["message"]["attachments"][0]["kind"] == kind
    assert runtime.store.status_snapshot()["inbox_submitted"] == 1


def test_recording_above_the_bot_api_limit_is_reported_not_retried(tmp_path):
    big = {**VOICE, "file_size": BOT_API_DOWNLOAD_LIMIT + 1}
    event = parse_telegram_update(_update(voice=big), bot_account_id="9")
    attachment = event.message["attachments"][0]
    assert attachment["status"] == "too_large" and "20 MiB" in attachment["note"]
    runtime, submitter, clients = _runtime(tmp_path)
    runtime.store.commit_update(7, event)
    assert asyncio.run(runtime.process_one_inbox())
    # No download was even attempted: the event goes on with the reference and the reason.
    assert clients == [] or clients[0].downloads == []
    sent, staged = submitter.calls[0]
    assert staged == () and sent["message"]["attachments"][0]["status"] == "too_large"
    assert runtime.store.status_snapshot()["inbox_submitted"] == 1


def test_at_the_limit_is_still_downloaded(tmp_path):
    exact = {**VOICE, "file_size": BOT_API_DOWNLOAD_LIMIT}
    event = parse_telegram_update(_update(voice=exact), bot_account_id="9")
    assert "status" not in event.message["attachments"][0]
    runtime, submitter, _clients = _runtime(tmp_path)
    runtime.store.commit_update(7, event)
    assert asyncio.run(runtime.process_one_inbox())
    assert [pathlib.Path(path).name for path in submitter.calls[0][1]] == ["0-voice-v-uniq.ogg"]
