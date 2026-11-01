"""Telegram group discovery through the real host, the plugin's own poll/capture and the owner's browser.

Raw synthetic Bot API updates go through the running plugin (its client, poll loop, parser and custody); the test
reads only public surfaces: the plugin's ``status`` route (``connect`` rows ``{account_id, conversation_id,
label}``), the core's ``/api/places``, and the page. It never opens the plugin's tables.

Every case sets up what it needs through ``world`` (idempotent, on the one module host, a fresh host per engine)
and asserts against the identities ``world`` has fed, so a case runs alone or after any other. The empty, unknown
and unreadable states live in ``test_empty_and_unknown_browser.py`` (they need a host with no groups) and the
update case in ``test_legacy_update_browser.py``. Against the published skill 0.8, which has no
``status.connect``, this file is the negative control: it fails where the rows should stand.
"""
from __future__ import annotations

import json

import pytest

import ui
import updates as U
from fake_bot_api import BOT_ID

EXISTING_GROUP = (-1001000000001, "Existing Active group")
EXISTING_DM = (7003, "Existing Active DM")
DUP_A, DUP_B = (-1002000000001, "Product sync"), (-1002000000002, "Product sync")
LONG = (-1002000000003, "Quarterly planning for the platform, onboarding and partner integrations team, "
                        "including the very long agenda that keeps going well past one line of a narrow phone")
CJK = (-1002000000004, "Команда разработки 🚀 開発チーム")
FORUM = (-1002000000005, "Forum with topics")
CHANNEL = (-1003000000001, "Announcements channel")
NEW_DM = U.OTHER
CHOOSABLE = [DUP_A, DUP_B, LONG, CJK, FORUM]
# membership: the bot itself added, removed and re-added, by a membership update and by service messages
META = (-1002000000099, "Membership only group")
SERVICE = (-1002000000096, "Joined by service message")
OLD_BASIC = (-1002000000098, "Old basic group")        # a basic group ...
MIGRATED = (-1002000000097, "Old basic group")         # ... that became this supergroup
MANY = [(-1004000000000 - n, f"Group number {n:03d}") for n in range(150)]
SETTINGS = ("state", "wakes", "notes", "revision", "label", "owner_accounts", "working_folder", "model", "account_id",
            "conversation_id")
# inbox_retained overlaps submitted rows; only these four states are disjoint.
COUNTS = ("inbox_waiting", "inbox_leased", "inbox_submitted", "inbox_failed")
BOT = {"id": BOT_ID, "is_bot": True, "first_name": "Presence Group Test"}


def view(place: dict) -> dict:
    return {key: place.get(key) for key in SETTINGS}


class World:
    """One host with the owner's two existing Active places and the arrivals fed so far.

    ``expected`` is the set of identities the offer must hold right now, kept by every step that feeds updates.
    """

    def __init__(self, host):
        self.host = host
        self.group = host.connect_place(str(EXISTING_GROUP[0]), EXISTING_GROUP[1], active=True)
        self.dm = host.connect_place(str(EXISTING_DM[0]), EXISTING_DM[1], active=True)
        ids = (self.group["place_id"], self.dm["place_id"])
        self.existing = {p["place_id"]: view(p) for p in host.places() if p["place_id"] in ids}
        assert len(self.existing) == 2 and all(v["state"] == "active" for v in self.existing.values())
        self.expected: set = set()
        self.arrived = self.crowded = False

    def feed(self, *updates) -> None:
        self.host.bot.wait_acked(max(self.host.bot.push(update) for update in updates))

    def arrivals(self) -> None:
        if self.arrived:
            return
        self.feed(
            U.group_message(*EXISTING_GROUP, "the existing room still talks"),
            U.group_message(*DUP_A, "first of two rooms named alike"),
            U.group_message(*DUP_B, "second of two rooms named alike"),
            U.group_message(*LONG, "a very long title"),
            U.group_message(*CJK, "привет, 你好, こんにちは"),
            U.group_message(*FORUM, "general", topic=None),
            U.group_message(*FORUM, "in topic 11", topic=11),
            U.group_message(*FORUM, "in topic 12", topic=12),
            U.private_message(NEW_DM, "hello from someone who opened a private chat"),
            U.channel_post(*CHANNEL),
        )
        self.expected |= {ui.exact(chat) for chat, _ in [EXISTING_GROUP, *CHOOSABLE]}
        self.arrived = True

    def crowd(self) -> None:
        if not self.crowded:
            self.feed(*(U.group_message(chat, title, "hello") for chat, title in MANY))
            self.expected |= {ui.exact(chat) for chat, _ in MANY}
            self.crowded = True

    def place_of(self, chat: int):
        return next((p for p in self.host.places() if p["conversation_id"] == str(chat)), None)

    def offered(self) -> list:
        return ui.connect_rows(self.host.status())

    def offered_row(self, chat: int):
        found = [r for r in self.offered() if r["conversation_id"] == str(chat)]
        assert len(found) <= 1, f"chat {chat} offered twice: {found}"
        return found[0] if found else None

    def unchanged_existing(self) -> None:
        now = {p["place_id"]: view(p) for p in self.host.places()}
        for place_id, was in self.existing.items():
            assert now[place_id] == was, f"the existing Active place {was['label']} changed"


@pytest.fixture(scope="module")
def world(host):
    return World(host)


# --- provenance ----------------------------------------------------------------------------------------
def test_run_is_the_real_host_with_a_controlled_provider(host, world, record):
    """The server is this candidate's, the redirect ran in it, and only the controlled Bot API was reached."""
    pids = set(host.provenance["server_pids"])
    assert pids & {p["pid"] for p in host.provenance["shim_proof"]}, "the redirect did not load in the server process"
    methods = {c["method"] for c in host.bot.calls}
    assert {"getMe", "deleteWebhook", "getUpdates"} <= methods and all(c["token_ok"] for c in host.bot.calls)
    assert host.bot.sends == []
    record(f"provenance-{host.engine}", {**host.provenance, "bot_methods": sorted(methods), "allowed_updates": next(
        c["body"]["allowed_updates"] for c in host.bot.calls if c["method"] == "getUpdates")})


# --- groups by exact identity -------------------------------------------------------------------------
def test_groups_are_offered_by_exact_identity(host, world, page, record):
    world.arrivals()
    placed = {p["place_id"] for p in host.places()}
    offered = world.offered()
    record(f"offered-status-{host.engine}", offered)
    identities = [ui.identity(r) for r in offered]
    assert len(identities) == len(set(identities)), "a chat is offered twice (a topic is not a chat)"
    assert set(identities) == world.expected, (f"offered {sorted(identities)}; no private chat, channel or anyone else's "
                                               f"chat may be offered: {sorted(set(identities) ^ world.expected)}")
    assert all(isinstance(r["label"], str) and set(r) >= {"account_id", "conversation_id", "label"} for r in offered)
    by_id = {r["conversation_id"]: r["label"] for r in offered}
    for chat, title in [EXISTING_GROUP, *CHOOSABLE]:
        label = by_id[str(chat)]
        assert ui.names_id(label, chat), f"label {label!r} lacks the exact signed numeric id {chat}"
        assert title[:40] in label, f"label {label!r} lacks the title {title[:40]!r}"
    assert by_id[str(DUP_A[0])] != by_id[str(DUP_B[0])], "two same-named groups must stay tellable apart"
    assert str(NEW_DM["id"]) not in by_id and str(CHANNEL[0]) not in by_id

    ui.open_connect(page, host.url)
    listing = ui.telegram_rows(page)
    assert len(listing) == len(world.expected), [r["label"] for r in listing]
    for chat, title in CHOOSABLE:
        row = ui.row_with_id(listing, chat)
        assert title[:40] in row["label"]
        if not world.place_of(chat):
            assert row["act"] == "connect" and row["button"] == "Connect"
    existing = ui.row_with_id(listing, EXISTING_GROUP[0])
    assert existing["act"] == "open" and existing["placeId"] == world.group["place_id"] and "Connected" in existing["text"]
    assert {r["label"] for r in ui.rows(page) if r["choice"] == "whole"} == {"Whole account"}
    record(f"offered-ui-{host.engine}", {"rows": listing, "shots": page.shots("offered")})
    world.unchanged_existing()
    assert host.bot.sends == [] and {p["place_id"] for p in host.places()} == placed


# --- one group, Silent, nothing else --------------------------------------------------------------------
def test_connect_only_the_chosen_group_and_it_starts_silent(host, world, page, record):
    world.arrivals()
    before = {p["place_id"] for p in host.places()}
    assert world.place_of(DUP_B[0]) is None
    ui.open_connect(page, host.url)
    target = ui.row_with_id(ui.telegram_rows(page), DUP_B[0])
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/places")) as sent:
        ui.press(page, target)
    body = json.loads(sent.value.post_data)
    assert (body["connector"], body["account_id"], body["conversation_id"]) == ("telegram-bot", str(BOT_ID), str(DUP_B[0])), body
    sheet = ui.place_sheet(page)
    assert sheet["state"] == ["silent"], sheet
    record(f"connected-sheet-{host.engine}", {"sheet": sheet, "shots": page.shots("connected-silent")})

    added = [p for p in host.places() if p["place_id"] not in before]
    assert [(p["account_id"], p["conversation_id"], p["state"]) for p in added] == [(str(BOT_ID), str(DUP_B[0]), "silent")], \
        f"exactly the chosen group must be connected, Silent: {[p['label'] for p in added]}"
    assert world.place_of(DUP_A[0]) is None, "the same-named group was connected with it"
    world.unchanged_existing()
    assert host.bot.sends == [], "a Silent place sent something to Telegram"


# --- Open again and again: nothing is rewritten ---------------------------------------------------------
def test_repeat_open_preserves_the_places_settings(host, world, page, record):
    world.arrivals()
    place = world.place_of(CJK[0]) or host.connect_place(str(CJK[0]), CJK[1])  # connected as an owner earlier did
    assert place["state"] == "silent"
    status, saved = host.post_status(f"/api/places/{place['place_id']}", {"wakes": ["mention"]})
    assert status == 200 and saved.get("ok"), saved
    if not saved["place"]["notes"]:
        status, saved = host.post_status(f"/api/places/{place['place_id']}", {"add_note": "A synthetic owner note for this room."})
        assert status == 200 and saved.get("ok"), saved
    settled = view(world.place_of(CJK[0]))
    assert settled["state"] == "silent" and [w["fact"] for w in settled["wakes"]] == ["mention"] and len(settled["notes"]) == 1

    writes, seen = [], []
    page.on("request", lambda r: writes.append(f"{r.method} {r.url}") if r.method != "GET" and "/api/" in r.url else None)
    for attempt in (1, 2):
        ui.open_connect(page, host.url)
        listing = ui.telegram_rows(page)
        row = ui.row_with_id(listing, CJK[0])
        assert row["act"] == "open" and row["placeId"] == place["place_id"] and "Connected" in row["text"], row
        assert ui.row_with_id(listing, LONG[0])["act"] == "connect", "an unconnected group looks connected"
        writes.clear()
        ui.press(page, row)
        sheet = ui.place_sheet(page)
        assert sheet["state"] == ["silent"] and sheet["wakes"] == ["mention"], sheet
        assert any("A synthetic owner note for this room." in n for n in sheet["notes"]), sheet["notes"]
        assert writes == [], f"Open wrote: {writes}"
        seen.append(page.shots(f"open-{attempt}"))
        ui.close_sheets(page)
        assert view(world.place_of(CJK[0])) == settled, "opening a connected group changed its settings or revision"
    world.unchanged_existing()
    assert host.bot.sends == []
    record(f"repeat-open-{host.engine}", {"settled": settled, "shots": seen})


# --- membership: the bot added, removed, re-added, migrated -----------------------------------------------
def test_membership_updates_are_requested_from_telegram(host, world):
    """A bot only receives ``my_chat_member`` when ``getUpdates`` asks for it; without it add/remove never arrive."""
    allowed = [c["body"]["allowed_updates"] for c in host.bot.calls if c["method"] == "getUpdates"]
    assert allowed and all("my_chat_member" in a for a in allowed[-3:]), allowed[-1]


def test_membership_and_service_updates_move_the_offer_and_create_no_place_wake_or_send(host, world, page, record):
    """Raw ``my_chat_member`` and service messages alone: add offers the exact row, remove withdraws it, re-add
    offers it again, a migration moves the offer to the supergroup and moves no Place. Read through the status
    route and the page; none of it adds an inbox row, connects a Place or sends. Earlier ordinary messages
    may independently retry and change inbox state or call the model on this shared host."""
    world.arrivals()
    def inbox_total() -> int:
        status = host.status()
        return sum(status[k] for k in COUNTS)

    before = {"inbox_total": inbox_total()}
    steps = {}

    def present(chat: int, title: str, step: str) -> None:
        row = world.offered_row(chat[0] if isinstance(chat, tuple) else chat)
        assert row is not None, f"{step}: the group is not offered"
        assert row["account_id"] == str(BOT_ID) and ui.names_id(row["label"], chat[0]) and title[:40] in row["label"], row
        steps[step] = row

    def absent(chat: int, step: str) -> None:
        assert world.offered_row(chat) is None, f"{step}: the group is still offered"
        steps[step] = None

    # by my_chat_member
    assert world.offered_row(META[0]) is None
    world.feed(U.membership(*META, BOT, was="left", now="member"))
    present(META, META[1], "meta-add")
    world.feed(U.membership(*META, BOT, was="member", now="kicked"))
    absent(META[0], "meta-remove")
    world.feed(U.membership(*META, BOT, was="kicked", now="administrator"))
    present(META, META[1], "meta-readd")
    # by service messages
    world.feed(U.service(*SERVICE, new_chat_members=[BOT]))
    present(SERVICE, SERVICE[1], "service-add")
    world.feed(U.service(*SERVICE, left_chat_member=BOT))
    absent(SERVICE[0], "service-remove")
    world.feed(U.service(*SERVICE, new_chat_members=[BOT]))
    present(SERVICE, SERVICE[1], "service-readd")
    # only groups and supergroups: a channel and a private chat that blocked the bot are never offered
    world.feed(U.membership(*CHANNEL, BOT, was="left", now="administrator", kind="channel"),
               U.membership(NEW_DM["id"], "", BOT, was="member", now="kicked", kind="private"))
    absent(CHANNEL[0], "channel")
    absent(NEW_DM["id"], "private")
    # a basic group with a Place of its own migrates to a supergroup
    world.feed(U.membership(*OLD_BASIC, BOT, was="left", now="member", kind="group"))
    present(OLD_BASIC, OLD_BASIC[1], "migration-source-before")
    owned = host.connect_place(str(OLD_BASIC[0]), OLD_BASIC[1])
    owned_view = view(owned)
    places = {p["place_id"] for p in host.places()}
    world.feed(U.service(*OLD_BASIC, kind="group", migrate_to_chat_id=MIGRATED[0]),
               U.service(*MIGRATED, migrate_from_chat_id=OLD_BASIC[0]))
    absent(OLD_BASIC[0], "migration-source-after")
    present(MIGRATED, MIGRATED[1], "migration-target")
    assert {p["place_id"] for p in host.places()} == places, "a migration connected or removed a Place"
    assert view(world.place_of(OLD_BASIC[0])) == owned_view, "the source group's Place changed"
    assert world.place_of(MIGRATED[0]) is None, "the Place moved to the supergroup by itself"

    world.expected |= {ui.exact(META[0]), ui.exact(SERVICE[0]), ui.exact(MIGRATED[0])}
    assert {ui.identity(r) for r in world.offered()} == world.expected
    after = {"inbox_total": inbox_total()}
    assert after == before, "a membership or service update added an inbox row"
    assert host.bot.sends == []
    world.unchanged_existing()

    ui.open_connect(page, host.url)
    listing = ui.telegram_rows(page)
    assert len(listing) == len(world.expected), [r["label"] for r in listing]
    for chat, title in (META, SERVICE, MIGRATED):
        assert ui.row_with_id(listing, chat)["act"] == "connect"
    for chat in (OLD_BASIC[0], CHANNEL[0]):
        assert not [r for r in listing if ui.names_id(r["label"], chat)], f"{chat} is still offered on the page"
    record(f"membership-{host.engine}", {"steps": steps, "before": before, "after": after, "shots": page.shots("membership")})


# --- no silent cap ---------------------------------------------------------------------------------------
def test_every_known_group_is_offered_without_a_silent_cap(host, world, page, record):
    world.arrivals()
    world.crowd()
    offered = world.offered()
    identities = {ui.identity(r) for r in offered}
    assert identities == world.expected and len(offered) == len(world.expected), (
        len(offered), len(world.expected), sorted(identities ^ world.expected)[:5])
    ui.open_connect(page, host.url)
    listing = ui.telegram_rows(page)
    assert len(listing) == len(world.expected)
    for chat, title in MANY:
        assert ui.row_with_id(listing, chat)["act"] == "connect"
    record(f"many-{host.engine}", {"rows": len(listing), "shots": page.shots("many")})
