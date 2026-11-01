"""Owner words are literal connector facts, never addressing or authority."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostClient
from telegram_bot.runtime import TelegramTransportRuntime
from test_plugin import FakeApi, FakeRequest
from test_runtime import Logger, RuntimeClient


SKILL_ROOT = Path(__file__).resolve().parents[1]
SIGNAL = {"id": "signal.words", "label": "Words you chose appear (connector setting)"}


@pytest.fixture
def plugin(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "telegram_words_fixture", SKILL_ROOT / "plugin.py",
        submodule_search_locations=[str(SKILL_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    yield module, api
    api.unload()


def update(text, *, kind="message", field="text", is_bot=False):
    return {"update_id": 42, kind: {
        "message_id": 7, "date": 1_700_000_000, field: text,
        "from": {"id": 3, "is_bot": is_bot, "username": "Joseph"},
        "chat": {"id": -42, "type": "channel" if "channel" in kind else "supergroup", "title": "Joseph"},
    }}


@pytest.mark.parametrize("words,text,match", [
    (["Joseph"], "Hello, Joseph!", True),
    (["Joseph"], "Hello, someone else!", False),
    (["JOSEPH"], "hello joseph", True),
    (["Joseph"], "Josephine", False),
    (["Joseph"], "xJoseph Josephx _Joseph Joseph_", False),
    (["Joseph"], "2Joseph Joseph2", False),
    (["Антон"], "Привет, АНТОН!", True),
    (["Straße"], "STRASSE", True),
    (["Joseph Steuer"], "Hello JOSEPH STEUER!", True),
    (["Joseph Steuer"], "Joseph  Steuer", False),
    (["Bora Bora"], "xBora Bora Bora", True),
    (["a.b"], "(A.B)", True),
    (["a.b"], "aXb", False),
    (["[name]"], "Hello [NAME]!", True),
    (["[name]"], "Hello name!", False),
    (["missing", "Joseph"], "Joseph", True),
    ([], "Joseph", False),
    ([""], "Joseph", False),
])
def test_literal_words(words, text, match):
    event = parse_telegram_update(update(text), bot_account_id="9", places=True, words=words)
    assert event is not None and event.text == text
    assert event.facts == ["any_message", *(["signal.words"] if match else [])]
    assert event.actor["platform_actor_id"] == "3"


@pytest.mark.parametrize("kind", ["message", "edited_message", "channel_post", "edited_channel_post"])
@pytest.mark.parametrize("field", ["text", "caption"])
@pytest.mark.parametrize("text,match", [("Joseph!", True), ("Someone else", False)])
def test_supported_message_text_and_captions(kind, field, text, match):
    wire = update(text, kind=kind, field=field)
    wire[kind]["reply_to_message"] = {"message_id": 6, "text": "Joseph", "from": {"id": 4}}
    event = parse_telegram_update(wire, bot_account_id="9", places=True, words=["Joseph"])
    assert event is not None
    assert ("signal.words" in event.facts) is match  # sender, chat and reply text are not searched


def test_reactions_have_no_message_text_to_match():
    wire = {"update_id": 43, "message_reaction": {
        "message_id": 7, "date": 1_700_000_000,
        "user": {"id": 3, "first_name": "Joseph"},
        "chat": {"id": -42, "type": "supergroup", "title": "Joseph"},
        "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}],
    }}
    event = parse_telegram_update(wire, bot_account_id="9", places=True, words=["Joseph", "👍"])
    assert event is not None and event.text == "" and event.facts == []


def test_manifest_and_settings_offer_words_without_suggesting_the_signal(plugin, tmp_path):
    module, api = plugin
    manifest = yaml.safe_load((SKILL_ROOT / "SKILL.md").read_text().split("---", 2)[1])
    place = manifest["contributes"][0]
    assert place["facts"] == ["any_message", "direct_message", "mention", "reply_to_me", "from_bot", SIGNAL]
    assert place["wakes"] == ["direct_message", "mention", "reply_to_me"]
    section = dict(api.settings)[place["settings"]]
    form = section["schema"]["components"][0]
    assert form["route"] == "settings/save" and form["method"] == "POST"
    words = next(field for field in form["fields"] if field["name"] == "words")
    assert words == {"name": "words", "label": "Words to notice (one per line)", "type": "textarea"}
    routes = {name: handler for name, handler, _methods in api.routes}
    current = asyncio.run(routes["settings/save"](FakeRequest(method="GET")))
    assert json.loads(current.body) == {"management_group_id": "", "words": ""}
    assert not (tmp_path / "settings.json").exists()
    status = json.loads(asyncio.run(module._status_route(None)).body)
    assert "labels" not in status and "words" not in status and "signal.words" not in json.dumps(status)


@pytest.mark.parametrize("value,stored", [
    (" Joseph \n\n A.B\r\nАнтон ", ["Joseph", "A.B", "Антон"]),
    ([" Joseph ", "", "A.B", "  "], ["Joseph", "A.B"]),
    ("", []), ([], []),
])
def test_settings_round_trip_and_partial_save(plugin, tmp_path, value, stored):
    module, _api = plugin
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"binding_id": "a" * 32, "management_group_id": "-42", "words": ["old"]}))
    response = asyncio.run(module._settings_save_route(FakeRequest({"words": value})))
    assert response.status_code == 200
    assert json.loads(path.read_text()) == {"binding_id": "a" * 32, "management_group_id": "-42", "words": stored}
    current = asyncio.run(module._settings_save_route(FakeRequest(method="GET")))
    assert json.loads(current.body) == {"management_group_id": "-42", "words": "\n".join(stored)}
    assert asyncio.run(module._settings_save_route(FakeRequest({"management_group_id": "-43"}))).status_code == 200
    assert json.loads(path.read_text())["words"] == stored


@pytest.mark.parametrize("value", [None, 42, True, {}, ["Joseph", 5], [["Joseph"]]])
def test_invalid_words_leave_saved_settings_intact(plugin, tmp_path, value):
    module, _api = plugin
    path = tmp_path / "settings.json"
    saved = json.dumps({"management_group_id": "-42", "words": ["Joseph"]})
    path.write_text(saved)
    response = asyncio.run(module._settings_save_route(FakeRequest({"words": value, "management_group_id": "-43"})))
    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "Words must be a list of plain strings"
    assert path.read_text() == saved


@pytest.mark.parametrize("advertised", [None, 0, 1])
@pytest.mark.parametrize("words", [["Joseph"], []])
def test_poll_persists_words_before_submission_and_negotiates_the_host(plugin, tmp_path, advertised, words):
    module, _api = plugin
    captured = []

    class Transport:
        async def request_json(self, method, url, **kwargs):
            if url.endswith("/identity"):
                return {"places_version": 1, **({"presence_wake_signals": advertised} if advertised is not None else {})}
            captured.append(kwargs["payload"]["event"])
            return {"status": "completed", "outcome": "silent"}

    async def run():
        assert (await module._settings_save_route(FakeRequest({"words": words, "management_group_id": "-42"}))).status_code == 200
        host = PresenceHostClient(state_dir=tmp_path, token_provider=lambda: "fixture",
                                  host_base="http://127.0.0.1:8767", transport=Transport())

        class OnePoll(RuntimeClient):
            async def get_updates(self, **_kwargs):
                runtime.stop()
                return [update("Hello JOSEPH!")]

        runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "fixture",
                                           logger=Logger(), submitter=host, client_factory=OnePoll)
        await runtime._poll_loop()
        lease = runtime.store.claim_inbox()
        assert lease is not None
        assert lease.payload["facts"] == ["any_message", *(["signal.words"] if words else [])]
        assert lease.payload["conversation"]["configured_room"] == "management_group"
        # Retrying the captured event after an edit keeps its original facts.
        assert (await module._settings_save_route(FakeRequest({"words": [] if words else ["Joseph"]}))).status_code == 200
        runtime.store.release_inbox(lease.event_id, reason="fixture retry", retry_after_sec=0)
        assert await runtime.process_one_inbox()
        assert captured[0]["facts"] == ["any_message", *(["signal.words"] if words and advertised == 1 else [])]
        assert runtime.store.status_snapshot()["inbox_submitted"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("text,wakes,is_bot,decision", [
    ("Joseph", [], False, "silent"),
    ("Joseph", ["signal.words"], False, "focus"),
    ("Josephine", ["signal.words"], False, "silent"),
    ("Someone else", ["signal.words"], False, "silent"),
    ("Joseph", ["signal.words"], True, "silent"),
    ("Joseph", ["signal.words", "from_bot"], True, "focus"),
    ("Someone else", ["signal.words", "from_bot"], True, "focus"),
])
def test_word_fact_through_silent_place_intake(tmp_path, monkeypatch, text, wakes, is_bot, decision):
    places = pytest.importorskip("ouroboros.places")
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
    from ouroboros.extension_contributions import project_contributions
    from ouroboros.extension_ui_validation import validate_contributions
    from ouroboros.place_intake import decide_attention
    from ouroboros.presence_admission import admit_presence_turn
    from ouroboros.presence_context import focus_metadata
    from ouroboros.presence_runner import PresenceTurnEvent, capture_presence_event

    manifest = parse_skill_manifest_text((SKILL_ROOT / "SKILL.md").read_text())
    declarations, errors = validate_contributions(manifest, skill_dir=SKILL_ROOT)
    assert errors == []
    rows = project_contributions("telegram-bot", declarations, "reviewed")
    monkeypatch.setattr("ouroboros.extension_registry_state.live_contribution_projection", lambda: rows)
    monkeypatch.setattr("ouroboros.presence_admission.focus_enabled", lambda _root: True)
    monkeypatch.setattr("ouroboros.presence_admission.focus_allowance", lambda _root: {"status": "available"})
    place = places.connect_place(tmp_path, connector="telegram-bot", provider="telegram", account_id="9",
                                 conversation_id="-42", state="silent", wakes=declarations[0]["wakes"])
    places.edit_place(tmp_path, place.place_id, "set_wakes", author="owner", wakes=wakes)
    parsed = parse_telegram_update(update(text, is_bot=is_bot), bot_account_id="9", places=True, words=["Joseph"])
    event = PresenceTurnEvent(**parsed.to_dict())
    admission = admit_presence_turn(drive_root=tmp_path, authenticated_transport_skill="telegram-bot",
                                    provider="telegram", account_id="9", conversation_id="-42")
    capture_presence_event(admission, event, drive_root=tmp_path)
    assert decide_attention(tmp_path, admission, event)[0] == decision
    focus = focus_metadata(admission, event, "fixture", 1)["focus"]
    assert not focus["addressed"] and not focus["actor"]["owner_confirmed"]
    assert places.get_place(tmp_path, place.place_id).state == "silent"
