"""Every inbound kind is an event: files are attachment records, the rest a non_text description (0.12.0).

event.text stays the person's own words or caption; nothing is made up. Synthetic updates only.
"""
import asyncio
import json
import sqlite3
from contextlib import contextmanager

import pytest

from telegram_bot.custody import _REMEMBER_MESSAGE, CustodyStore
from telegram_bot.events import BOT_API_DOWNLOAD_LIMIT, NON_TEXT_KEY, parse_telegram_update
from telegram_bot.host import _EVENT_KEYS
from telegram_bot.runtime import TelegramTransportRuntime

from fake_bot_api import FakeBotApi
from test_runtime import AcceptingSubmitter, Logger

CHAT = {"id": -100777, "type": "supergroup", "title": "Room"}


def _update(update_id=7, *, edited=False, **message):
    body = {"message_id": 40 + update_id, "date": 1_700_000_000, "from": {"id": 3, "is_bot": False, "first_name": "Ann"},
            "chat": dict(CHAT), **message}
    return {"update_id": update_id, ("edited_message" if edited else "message"): body}


def _event(update):
    event = parse_telegram_update(update, bot_account_id="9", places=True, bot_username="presence_bot")
    assert event is not None
    assert set(event.to_dict()) - {"facts"} == _EVENT_KEYS  # top-level keys frozen: everything new is in message
    return event


VIDEO = {"file_id": "vid", "file_unique_id": "uv", "duration": 12, "width": 640, "height": 360,
         "mime_type": "video/mp4", "file_name": "clip.mp4", "file_size": 2_000_000}
NOTE = {"file_id": "note", "file_unique_id": "un", "duration": 9, "length": 384, "file_size": 700_000}
GIF = {"file_id": "gif", "file_unique_id": "ug", "duration": 3, "width": 320, "height": 240,
       "mime_type": "video/mp4", "file_name": "cat.gif.mp4", "file_size": 300_000}


@pytest.mark.parametrize("field,item,kind,name,caption", [
    ("video", VIDEO, "video", "clip.mp4", "watch this"),
    ("video", VIDEO, "video", "clip.mp4", ""),  # caption-less: 0.11 dropped it
    ("video_note", NOTE, "video_note", "video_note-un.mp4", ""),
    ("animation", GIF, "animation", "cat.gif.mp4", ""),
])
def test_video_video_note_and_animation_are_downloaded_files_captioned_or_not(tmp_path, field, item, kind, name, caption):
    update = _update(**{field: item}, **({"caption": caption} if caption else {}))
    if field == "animation":
        update["message"]["document"] = {k: item[k] for k in ("file_id", "file_unique_id", "file_name", "mime_type")}
    event = _event(update)
    assert event.text == caption and NON_TEXT_KEY not in event.message
    [record] = event.message["attachments"]  # an animation is not also staged as its compatibility document
    assert (record["kind"], record["file_name"], record["mime_type"], record["file_size"]) == (
        kind, name, item.get("mime_type", "video/mp4"), item["file_size"])
    assert record["duration_sec"] == item["duration"] and "status" not in record
    api = FakeBotApi()
    api.files[item["file_id"]] = bytes(item["file_size"])
    submitter = AcceptingSubmitter()
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                       submitter=submitter, client_factory=api.client)
    runtime.store.commit_update(7, event)
    assert asyncio.run(runtime.process_one_inbox())
    sent, staged = submitter.calls[0]
    assert [path.name for path in staged] == [f"0-{name}"]
    assert sent["message"]["attachments"][0]["status"] == "staged" and sent["text"] == caption
    assert [fields["file_id"] for fields in api.methods("getFile")] == [item["file_id"]]


def test_a_video_above_the_limit_is_too_large_and_never_fetched():
    event = _event(_update(video={**VIDEO, "file_size": BOT_API_DOWNLOAD_LIMIT + 5_000_000}, caption="big one"))
    [record] = event.message["attachments"]
    assert record["status"] == "too_large" and record["file_id"] == "vid" and event.text == "big one"
    assert record["note"] == "26 MB; the cloud Bot API gives bots files only up to 20 MB"


NON_TEXT = [
    ("sticker", {"file_id": "st", "file_unique_id": "us", "type": "regular", "width": 512, "height": 512,
                 "is_animated": True, "is_video": False, "emoji": "😂", "set_name": "CatPack"},
     "sticker", '😂 animated sticker from set "CatPack"'),
    ("poll", {"id": "p", "question": "Lunch?", "type": "quiz", "is_anonymous": True, "is_closed": False,
              "allows_multiple_answers": False, "total_voter_count": 0, "explanation": "Pizza wins",
              "options": [{"text": "Pizza", "voter_count": 0}, {"text": "Soup", "voter_count": 0}]},
     "poll", 'quiz poll "Lunch?" options: "Pizza"; "Soup" (anonymous) explanation: "Pizza wins"'),
    ("location", {"latitude": 55.75, "longitude": 37.62, "horizontal_accuracy": 20},
     "location", "latitude 55.75, longitude 37.62 (accuracy 20 m)"),
    ("venue", {"location": {"latitude": 55.75, "longitude": 37.62}, "title": "Cafe", "address": "Tverskaya 1"},
     "venue", '"Cafe" Tverskaya 1 (latitude 55.75, longitude 37.62)'),
    ("contact", {"phone_number": "+70000000000", "first_name": "Mira", "last_name": "Lee", "user_id": 7001},
     "contact", "Mira Lee +70000000000 (Telegram user 7001)"),
    ("dice", {"emoji": "🎲", "value": 4}, "dice", "🎲 rolled 4"),
    ("story", {"chat": {"id": -100555, "type": "channel", "title": "News", "username": "news"}, "id": 12},
     "story", 'story 12 from "News" @news (chat -100555)'),
    ("game", {"title": "Snake", "description": "Eat the dots", "photo": []}, "game", 'game "Snake" Eat the dots'),
    ("invoice", {"title": "Ticket", "description": "Row 5", "start_parameter": "t", "currency": "EUR",
                 "total_amount": 1500}, "invoice", 'invoice "Ticket" Row 5 total_amount 1500 EUR'),
    ("giveaway", {"chats": [], "winners_selection_date": 1_800_000_000, "winner_count": 3,
                  "prize_description": "A book"}, "giveaway", "giveaway 3 winners prize: A book"),
    ("checklist", {"title": "Trip", "tasks": [{"id": 1, "text": "Tickets"}, {"id": 2, "text": "Hotel"}]},
     "checklist", 'checklist "Trip" tasks: "Tickets"; "Hotel"'),
    ("rich_message", {"blocks": []}, "other", "Telegram rich message (not described by this connector)"),
]


@pytest.mark.parametrize("field,value,kind,description", NON_TEXT, ids=[row[0] for row in NON_TEXT])
def test_each_non_file_kind_is_described_and_text_stays_empty(field, value, kind, description):
    event = _event(_update(**{field: value}))
    assert event.text == ""  # never synthesized from the description
    assert event.message[NON_TEXT_KEY] == [{"kind": kind, "description": description}]
    assert event.message["attachments"] == []  # a sticker is described, not fetched
    assert event.facts == ["any_message"]


def test_paid_media_describes_a_preview_and_stages_what_telegram_gives_as_a_file():
    paid = {"star_count": 50, "paid_media": [
        {"type": "preview", "width": 1280, "height": 720},
        {"type": "photo", "photo": [{"file_id": "pp", "file_unique_id": "upp", "width": 90, "height": 90, "file_size": 900}]}]}
    event = _event(_update(paid_media=paid, caption="for subscribers"))
    assert event.text == "for subscribers"
    assert event.message[NON_TEXT_KEY] == [{"kind": "paid_media", "description":
                                            "paid media for 50 Telegram Stars (2 items: preview without a file 1280x720, photo)"}]
    assert [(record["kind"], record["file_id"]) for record in event.message["attachments"]] == [("photo", "pp")]


def test_a_live_location_refresh_is_not_another_message():
    live = {"latitude": 55.75, "longitude": 37.62, "live_period": 900}
    first = _event(_update(location=live))
    assert first.message[NON_TEXT_KEY][0]["description"] == "latitude 55.75, longitude 37.62 (live for 900 s)"
    refresh = parse_telegram_update(_update(8, edited=True, location={**live, "latitude": 55.76}, edit_date=1_700_000_060),
                                    bot_account_id="9", places=True)
    assert refresh is None


def test_a_first_channel_location_post_arrives_and_only_edits_of_a_location_are_dropped():
    channel = {"id": -100888, "type": "channel", "title": "News"}

    def post(kind, update_id, **extra):
        return {"update_id": update_id, kind: {"message_id": 60, "date": 1_700_000_000, "chat": dict(channel),
                                               "sender_chat": dict(channel),
                                               "location": {"latitude": 55.75, "longitude": 37.62}, **extra}}

    first = _event(post("channel_post", 7))  # a channel post is not an edit: it must not be lost
    assert first.message["event_kind"] == "channel_post" and first.text == ""
    assert first.message[NON_TEXT_KEY] == [{"kind": "location", "description": "latitude 55.75, longitude 37.62"}]
    for kind in ("edited_channel_post", "edited_message"):
        edit = post(kind, 8, edit_date=1_700_000_060)
        if kind == "edited_message":
            edit[kind]["chat"], edit[kind]["from"] = dict(CHAT), {"id": 3, "is_bot": False, "first_name": "Ann"}
            del edit[kind]["sender_chat"]
        assert parse_telegram_update(edit, bot_account_id="9", places=True) is None, kind


def test_a_live_photo_keeps_its_photo_and_says_what_else_it_was():
    event = _event(_update(photo=[{"file_id": "lp", "file_unique_id": "ulp", "width": 90, "height": 90}],
                           live_photo={"photo": [], "video": {"file_id": "lv"}}))
    assert [record["kind"] for record in event.message["attachments"]] == ["photo"]
    assert event.message[NON_TEXT_KEY] == [{"kind": "other",
                                            "description": "Telegram live photo (not described by this connector)"}]


def test_album_position_is_counted_across_poll_batches_and_kept_on_edits(tmp_path):
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=Logger(),
                                       submitter=AcceptingSubmitter())
    photo = [{"file_id": "a", "file_unique_id": "ua", "file_size": 10}]
    for update_id in (7, 8, 9):  # three updates, as Telegram may split one album across getUpdates answers
        runtime.store.commit_update(update_id, _event(_update(update_id, media_group_id="g1", photo=photo)))
    edit = _update(10, edited=True, media_group_id="g1", photo=photo, caption="fix", edit_date=1_700_000_100)
    edit["edited_message"]["message_id"] = 48  # the second member, edited later
    runtime.store.commit_update(10, _event(edit))
    runtime.store.commit_update(11, _event(_update(11, media_group_id="g2", photo=photo)))
    positions = []
    while (lease := runtime.store.claim_inbox()) is not None:
        positions.append((lease.payload["message"]["message_id"], lease.payload["message"]["media_group_position"]))
        runtime.store.mark_inbox_failed(lease.event_id, reason="test")
    assert positions == [(47, 1), (48, 2), (49, 3), (48, 2), (51, 1)]


class _Stepped(CustodyStore):
    """The same store; its connection counts the SQLite VM steps it runs."""
    steps = 0

    @contextmanager
    def _connect(self):
        with super()._connect() as conn:
            conn.set_progress_handler(_Stepped.step, 1)
            yield conn

    @staticmethod
    def step():
        _Stepped.steps += 1
        return 0


def _positions(store):
    with sqlite3.connect(store.path) as db:
        return [json.loads(row[0])["message"]["media_group_position"]
                for row in db.execute("SELECT payload_json FROM inbox ORDER BY update_id")]


def _album_member(update_id, message_id):
    update = _update(update_id, media_group_id="g1", photo=[{"file_id": "a", "file_unique_id": "ua", "file_size": 10}])
    update["message"]["message_id"] = message_id
    return _event(update)


def test_an_album_members_position_reads_that_albums_earlier_members_not_the_chats_history(tmp_path):
    """However long the chat's retained history, a member's position is counted in a partial index of album rows."""
    from telegram_bot.custody import _ALBUM_EARLIER
    costs = {}
    for size in (10, 5000):
        store = _Stepped(tmp_path / str(size) / "custody.sqlite3")
        with sqlite3.connect(store.path) as db:  # the chat's earlier messages, every fourth in some other album
            db.executemany(_REMEMBER_MESSAGE, [("9", str(CHAT["id"]), message_id, "", 0, "3", "Ann", 0, "words",
                                                f"old-{message_id // 8}" if message_id % 4 == 0 else "")
                                               for message_id in range(1, size)])
        _Stepped.steps = 0
        for update_id in (7, 8, 9):
            store.commit_update(update_id, _album_member(update_id, 10 ** 6 + update_id))
        costs[size] = _Stepped.steps
        assert _positions(store) == [1, 2, 3]
        with sqlite3.connect(store.path) as db:
            plan = " ".join(str(row[3]) for row in db.execute(
                "EXPLAIN QUERY PLAN " + _ALBUM_EARLIER, ("9", str(CHAT["id"]), "g1", 10 ** 6 + 9)))
        assert "telegram_messages_album" in plan and "SCAN" not in plan, plan
    assert costs[5000] - costs[10] < 300, costs


def test_a_store_from_before_the_album_index_gains_it_on_open_with_the_members_already_there(tmp_path):
    store = CustodyStore(tmp_path / "custody.sqlite3")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP INDEX telegram_messages_album")  # the table as every earlier version left it
    for update_id in (7, 8):
        store.commit_update(update_id, _album_member(update_id, 40 + update_id))
    for _ in range(2):  # the first open builds it from the rows already there; the next finds it
        reopened = CustodyStore(store.path)
        with sqlite3.connect(store.path) as db:
            indexes = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='telegram_messages'")]
            indexed = db.execute("SELECT COUNT(*) FROM telegram_messages INDEXED BY telegram_messages_album "
                                 "WHERE media_group_id<>''").fetchone()[0]
        assert indexes.count("telegram_messages_album") == 1 and indexed == 2
    reopened.commit_update(9, _album_member(9, 49))
    assert _positions(reopened) == [1, 2, 3]


def test_forward_reply_quote_and_album_facts_stay_inside_message():
    origin = {"type": "user", "date": 1_699_999_000, "sender_user": {"id": 77, "is_bot": False, "first_name": "Bob"}}
    reply = {"message_id": 30, "date": 1_699_999_500, "chat": dict(CHAT), "from": {"id": 9, "is_bot": True},
             "poll": {"id": "p", "question": "Lunch?", "type": "regular", "options": [{"text": "Pizza"}]}}
    event = _event(_update(text="forwarded words", forward_origin=origin, is_automatic_forward=True,
                           quote={"text": "Lunch", "position": 0}, reply_to_message=reply))
    message = event.message
    assert event.text == "forwarded words" and event.actor["platform_actor_id"] == "3"  # the forwarder, not Bob
    assert message["forward_origin"] == origin and message["is_automatic_forward"] is True
    assert message["quote"] == {"text": "Lunch", "position": 0} and message["reply_to_message_id"] == 30
    assert message["reply_to_message"][NON_TEXT_KEY] == [{"kind": "poll", "description": 'regular poll "Lunch?" options: "Pizza"'}]
    assert "reply_to_me" in event.facts


def test_a_message_with_nothing_a_person_sent_is_still_not_an_event():
    # Service updates (here a title change) are not inbound content; group discovery handles membership.
    assert parse_telegram_update(_update(new_chat_title="Renamed"), bot_account_id="9", places=True) is None
