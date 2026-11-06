"""Negotiated place events and live policy at the physical-send boundary."""
import asyncio
import dataclasses
import json
import re
from pathlib import Path

import httpx
import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import PLACE_POLICY_UNREAD, SKILL_ACCESS_REFUSED, LoopbackPresenceHostAdapter, slack_presence_event
from lib.local_settings import NEW_ROOT_CHANNELS
from lib.presence import NEW_ROOT_SIGNAL
from lib.runtime import OutboundWorker
from lib.store import BridgeStore
from test_host_adapter import _item
from test_workers_plugin import _Api, _Slack, _load_plugin

_LISTED = frozenset({"C0STATUS01"})


def _place_declaration():
    import yaml
    text = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    return next(item for item in yaml.safe_load(text.split("---")[1])["contributes"] if item.get("provides") == "place")


def _declared_fact_ids():
    return {fact if isinstance(fact, str) else fact["id"] for fact in _place_declaration()["facts"]}


def _root(**fields):
    return {"type": "message", "user": "U1", "channel": "C0STATUS01", "channel_type": "channel",
            "ts": "20.0", "event_ts": "20.0", "text": "Rollout plan for stage 3", **fields}


_NEW_ROOT_CASES = {
    "listed root": (_root(), _LISTED, True),
    "listed root mention": (_root(type="app_mention", text="<@U_BOT> rollout plan"), _LISTED, True),
    "listed bot post": (_root(subtype="bot_message", user=None, bot_id="B_OTHER"), _LISTED, True),
    "listed file post": (_root(subtype="file_share", files=[{"id": "F1", "name": "plan.pdf"}]), _LISTED, True),
    "no list": (_root(), frozenset(), False),
    "another channel": (_root(channel="C0OTHER001"), _LISTED, False),
    "thread reply": (_root(thread_ts="19.0", parent_user_id="U2"), _LISTED, False),
    "reply also sent to channel": (_root(subtype="thread_broadcast", thread_ts="19.0"), _LISTED, False),
    "edit": ({"type": "message", "subtype": "message_changed", "channel": "C0STATUS01", "channel_type": "channel",
              "event_ts": "21.0", "message": {"user": "U1", "ts": "20.0", "text": "Rollout plan, revised"},
              "previous_message": {"user": "U1", "ts": "20.0", "text": "Rollout plan"}}, _LISTED, False),
    "mention added by an edit": (_root(type="app_mention", text="<@U_BOT> rollout plan",
                                       edited={"user": "U1", "ts": "21.0"}), _LISTED, False),
    "delete": ({"type": "message", "subtype": "message_deleted", "channel": "C0STATUS01", "channel_type": "channel",
                "ts": "22.0", "deleted_ts": "20.0",
                "previous_message": {"user": "U1", "ts": "20.0", "text": "Rollout plan"}}, _LISTED, False),
    "reaction": ({"type": "reaction_added", "user": "U1", "reaction": "eyes", "event_ts": "23.0",
                  "item": {"type": "message", "channel": "C0STATUS01", "ts": "20.0"}}, _LISTED, False),
    "join notice": (_root(subtype="channel_join", text="<@U1> has joined the channel"), _LISTED, False),
}


@pytest.mark.parametrize("case", _NEW_ROOT_CASES)
def test_new_root_signal_marks_only_new_top_level_posts_in_listed_channels(tmp_path, case):
    event, channels, expected = _NEW_ROOT_CASES[case]
    payload = {"type": "events_api", "envelope_id": "env-root",
               "payload": {"team_id": "T1", "event_id": "Ev-root", "event": event}}
    parsed = parse_socket_envelope(payload, bot_user_id="U_BOT", new_root_channels=channels)
    plain = parse_socket_envelope(payload, bot_user_id="U_BOT")
    # The list never changes admission; it only adds the one fact.
    assert parsed.accepted and (parsed.accepted, parsed.reason) == (plain.accepted, plain.reason)
    added = {key: value for key, value in parsed.event.structured.items() if key not in plain.event.structured}
    assert added == ({"new_root_signal": True} if expected else {})

    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parsed)
    item = store.claim_inbox()
    presence = slack_presence_event(item, places=True)
    assert presence["facts"].count(NEW_ROOT_SIGNAL) == int(expected)
    assert presence["message"]["provider_facts"].get("new_root_signal") is (True if expected else None)
    without = slack_presence_event(dataclasses.replace(item, structured=plain.event.structured), places=True)
    assert [fact for fact in presence["facts"] if fact != NEW_ROOT_SIGNAL] == without["facts"]
    # The Host refuses a fact the place does not declare; legacy hosts get no facts at all.
    assert set(presence["facts"]) <= _declared_fact_ids()
    assert "facts" not in slack_presence_event(item)


@pytest.mark.parametrize("channel,kind,thread", [
    # The signal is a fact only. A new top-level post in a channel opens its own thread, and the reply goes
    # there; in a direct or group chat it joins the chat's one conversation, and the reply goes to the chat.
    ("C0STATUS01", "channel", "20.0"), ("D0DIRECT01", "im", ""), ("G0GROUPS01", "mpim", ""),
])
def test_a_new_root_keeps_the_room_and_reply_thread_of_its_conversation(tmp_path, channel, kind, thread):
    payload = {"type": "events_api", "envelope_id": "env-root",
               "payload": {"team_id": "T1", "event_id": "Ev-root", "event": _root(channel=channel, channel_type=kind)}}
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT", new_root_channels={channel}))
    item = store.claim_inbox()
    presence = slack_presence_event(item, places=True)
    assert NEW_ROOT_SIGNAL in presence["facts"]
    assert (presence["thread_id"], presence["conversation_key"], item.conversation_thread) == (
        thread, f"slack:T1:{channel}:{thread or '0'}", thread)


@pytest.mark.parametrize("advertised, sent", [(1, True), (None, False), (True, False), (2, False)])
def test_new_root_signal_reaches_only_a_host_that_vouches_for_signals(advertised, sent):
    async def run():
        submitted = []

        def handler(request):
            if request.url.path == "/identity":
                answer = {"places_version": 1}
                if advertised is not None:
                    answer["presence_wake_signals"] = advertised
                return httpx.Response(200, json=answer)
            submitted.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "completed", "outcome": "silent", "turn_ref": "t"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:8767",
                                               skill_token="test", http_client=http)
            await host.submit(dataclasses.replace(_item(), structured={"new_root_signal": True}))
        event = submitted[0]["event"]
        assert event["facts"] == (["any_message", NEW_ROOT_SIGNAL] if sent else ["any_message"])
        # The observation itself stays with the message either way.
        assert event["message"]["provider_facts"]["new_root_signal"] is True
    asyncio.run(run())


def test_place_declares_new_root_signal_as_opt_in_beside_every_reported_fact(tmp_path):
    place = _place_declaration()
    ids = [fact if isinstance(fact, str) else fact["id"] for fact in place["facts"]]
    assert len(ids) == len(set(ids))
    signal = next(fact for fact in place["facts"] if isinstance(fact, dict) and fact.get("id") == NEW_ROOT_SIGNAL)
    assert set(signal) == {"id", "label"} and re.fullmatch(r"signal\.[a-z0-9_-]{1,40}", signal["id"])
    assert signal["label"].strip() and len(signal["label"]) <= 120
    # Opt-in: the suggested wakes are unchanged and never include the signal.
    assert place["wakes"] == ["direct_message", "group_dm", "mention", "my_thread", "reply_to_me"]
    # The place sheet offers only declared facts, so each fact this bridge reports stays selectable.
    assert set(ids) == {"any_message", "direct_message", "group_dm", "mention", "my_thread", "reply_to_me",
                        "from_bot", NEW_ROOT_SIGNAL}
    # Its settings door names the section whose form holds the channel list.
    api = _Api(tmp_path)
    _load_plugin().register(api)
    _title, schema = api.settings[place["settings"]]
    form = next(component for component in schema["components"] if component["type"] == "form")
    assert NEW_ROOT_CHANNELS in [field["name"] for field in form["fields"]]
    try:
        from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
        from ouroboros.extension_ui_validation import validate_contributions, validate_settings_schema
    except ImportError:
        return  # The serving core's validators run only where that core is importable.
    root = Path(__file__).resolve().parents[1]
    clean, problems = validate_contributions(
        parse_skill_manifest_text((root / "SKILL.md").read_text(encoding="utf-8")), skill_dir=root)
    assert problems == []
    assert any(item.get("provides") == "place" and signal in item["facts"] for item in clean)
    validate_settings_schema(schema)

NOT_CONNECTED = {"ok": False, "error": "Connect this place in Consciousness > Places.",
                 "code": "place_not_connected", "disposition": "blocked"}
# The Host's 403 carries a diagnostic, not words for a person; only the owner can lift it in Skills.
ACCESS_REFUSED = {"ok": False, "error": "skill 'slack-bridge' lacks grant 'presence'",
                  "code": "presence_auth_blocked", "disposition": "blocked"}


def _place_hold(place, identity=None):
    """A real adapter whose Host answers /identity and /presence/place as told; an exception is raised."""
    async def run():
        def handler(request):
            answer = (identity or httpx.Response(200, json={"places_version": 1})) if request.url.path == "/identity" else place
            if isinstance(answer, BaseException):
                raise answer
            return answer
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:8767", skill_token="test", http_client=http)
            return await host.place_hold("T1", "C1")
    return asyncio.run(run())


@pytest.mark.parametrize("place", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
    httpx.Response(503, json={"ok": False, "code": "places_unavailable", "disposition": "retry"}),
    httpx.Response(404, text="not this Host's refusal"),
    httpx.Response(404, json=["not an object"]),
    httpx.Response(200, text="not JSON"),
    httpx.Response(200, json={"place_id": "p", "label": "Slack"}),  # an answer that names no state says nothing about sending
])
def test_unread_place_policy_is_held_in_words_that_ask_nothing(place):
    assert _place_hold(place) == PLACE_POLICY_UNREAD


def test_host_that_cannot_answer_identity_holds_in_the_same_words_and_older_hosts_do_not_hold():
    assert _place_hold(httpx.Response(200, json={}), identity=httpx.ConnectError("refused")) == PLACE_POLICY_UNREAD
    assert _place_hold(httpx.Response(200, json={}), identity=httpx.Response(200, json={"ok": True})) == ""


def test_place_state_reads_as_before_and_not_connected_keeps_the_hosts_words():
    assert _place_hold(httpx.Response(200, json={"state": "silent", "label": "Team room"})) == "Held: Team room is silent"
    assert _place_hold(httpx.Response(200, json={"state": "paused"})) == "Held: Slack is paused"
    assert _place_hold(httpx.Response(200, json={"state": "active"})) == ""
    assert _place_hold(httpx.Response(404, json=NOT_CONNECTED)) == "Held: Connect this place in Consciousness > Places."


@pytest.mark.parametrize("place,words", [
    (httpx.Response(403, json=ACCESS_REFUSED), SKILL_ACCESS_REFUSED),
    (httpx.Response(503, json={"ok": False, "code": "places_unavailable", "disposition": "retry"}), PLACE_POLICY_UNREAD),
])
def test_a_refused_skill_sends_its_owner_to_skills_and_an_unread_place_still_retries(place, words):
    assert _place_hold(place) == words


@pytest.mark.parametrize("line,by_itself,in_skills,cause", [
    (PLACE_POLICY_UNREAD, True, False, "did not answer"),
    (SKILL_ACCESS_REFUSED, False, True, "refused"),
])
def test_a_hold_says_whether_its_reader_acts_and_leaves_the_hosts_answer_out(line, by_itself, in_skills, cause):
    assert line.startswith("Held: ")  # the outbox counts a hold by its prefix
    assert ("by itself" in line) is by_itself and ("in Skills" in line) is in_skills
    assert cause not in line  # which answer the Host gave changes nothing the reader does


def test_slack_addressing_is_structured_and_legacy_payload_is_unchanged():
    item = dataclasses.replace(_item(), channel_type="mpim", thread_ts="9.0", structured={
        "self_user_id": "BOT", "mentioned_user_ids": ["BOT"], "parent_user_id": "BOT", "actor_is_bot": True,
    })
    event = slack_presence_event(item, places=True)
    assert set(event["facts"]) == {"any_message", "group_dm", "mention", "reply_to_me", "my_thread", "from_bot"}
    assert "facts" not in slack_presence_event(item)
    ordinary = dataclasses.replace(item, structured={}, thread_ts="", subtype="", channel_type="channel", text="mention BOT please")
    assert slack_presence_event(ordinary, places=True)["facts"] == ["any_message", "signal.new_root"]
    assert slack_presence_event(dataclasses.replace(ordinary, channel_type="im"), places=True)["facts"] == ["any_message", "signal.new_root", "direct_message"]
    deleted = dataclasses.replace(ordinary, channel_type="im", subtype="message_deleted")
    assert slack_presence_event(deleted, places=True)["facts"] == []


@pytest.mark.parametrize("change", [
    {"thread_ts": "9.0"},
    {"subtype": "file_share"},
    {"subtype": "message_changed"},
    {"subtype": "message_deleted"},
])
def test_new_root_signal_excludes_replies_edits_deletes_and_subtypes(change):
    item = dataclasses.replace(_item(), thread_ts="", subtype="", structured={})
    item = dataclasses.replace(item, **change)
    assert "signal.new_root" not in slack_presence_event(item, places=True)["facts"]


def test_places_host_accepts_unbound_events_and_checks_state_each_time():
    async def run():
        states = iter(["silent", "paused", "active"])
        submitted = []
        def handler(request):
            assert request.headers["x-skill-token"] == "test"
            if request.url.path == "/identity":
                return httpx.Response(200, json={"places_version": 1})
            if request.url.path == "/presence/place":
                assert dict(request.url.params) == {"provider": "slack", "account_id": "T1", "conversation_id": "C1"}
                return httpx.Response(200, json={"place_id": "p", "label": "Slack", "state": next(states)})
            if request.url.path == "/presence/work/retained":
                assert "binding_id" not in request.url.params
                if request.method == "POST":
                    assert json.loads(request.content) == {"transport_queue": {"pending": 1}}
                    return httpx.Response(200, json={"ok": True, "status": "recorded"})
                return httpx.Response(200, json={"status": "completed", "outcome": "silent"})
            submitted.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "completed", "outcome": "silent", "turn_ref": "t"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:8767", skill_token="test", http_client=http)
            await host.submit(_item())
            assert host.available and submitted[0]["event"]["facts"] == ["any_message"]
            assert "binding_id" not in submitted[0]
            assert "silent" in await host.place_hold("T1", "C1")
            assert "paused" in await host.place_hold("T1", "C1")
            assert await host.place_hold("T1", "C1") == ""
            assert (await host._get_work("retained"))["status"] == "completed"
            await host.refresh_transport_queue("retained", {"pending": 1})
    asyncio.run(run())


def test_place_lookup_names_a_thread_only_when_one_is_given():
    async def run():
        lookups = []
        def handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"places_version": 1})
            lookups.append(dict(request.url.params))
            return httpx.Response(200, json={"place_id": "p", "label": "Slack", "state": "active"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:8767", skill_token="test", http_client=http)
            assert await host.place_hold("T1", "C1", thread_id="1.0") == ""
            assert await host.place_hold("T1", "C1") == ""
        assert lookups == [{"provider": "slack", "account_id": "T1", "conversation_id": "C1", "thread_id": "1.0"},
                           {"provider": "slack", "account_id": "T1", "conversation_id": "C1"}]
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["Held: Slack is silent", "Held: Slack is paused", PLACE_POLICY_UNREAD])
def test_held_outbox_resumes_without_losing_identity_or_attempts(tmp_path, monkeypatch, reason):
    async def run():
        clock = [1000.0]
        monkeypatch.setattr("time.time", lambda: clock[0])
        class Host:
            held = True
            async def place_hold(self, account, conversation):
                assert (account, conversation) == ("T1", "C1")
                return reason if self.held else ""
        host, slack, store = Host(), _Slack(), BridgeStore(tmp_path)
        store.set_runtime(workspace_id="T1")
        store.enqueue_outbox(request_id="stable", target="C1", thread_ts="root", chunks=["bytes"])
        worker = OutboundWorker(store, slack, host)
        assert await worker.process_once()
        assert slack.posts == []
        part = store.delivery_receipt("stable")["parts"][0]
        assert part["attempts"] == 0 and part["error"] == reason and part["state"] == "held"
        assert store.status()["outbox_held"] == 1
        host.held = False
        clock[0] += 6
        assert await worker.process_once()
        assert slack.posts == [("C1", "bytes", "root")]
        assert store.delivery_receipt("stable")["parts"][0]["state"] == "delivered"
        assert not await worker.process_once()
    asyncio.run(run())


def test_upload_rechecks_after_bytes_before_publication(tmp_path, monkeypatch):
    from lib.slack_api import SlackClient
    async def run():
        clock = [1000.0]
        monkeypatch.setattr("time.time", lambda: clock[0])
        class Host:
            active = True
            async def place_hold(self, account, conversation):
                return "" if self.active else "Held: Slack is silent"
        host, store = Host(), BridgeStore(tmp_path)
        artifact = tmp_path / "file.txt"
        artifact.write_bytes(b"immutable")
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith("getUploadURLExternal"):
                return httpx.Response(200, json={"ok": True, "upload_url": "https://files.slack.com/upload", "file_id": "F1"})
            if request.url.path == "/upload":
                host.active = False
                return httpx.Response(200, text="OK")
            return httpx.Response(200, json={"ok": True, "files": [{"id": "F1"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            store.enqueue_mutation(request_id="file", operation="upload_file", payload={"path": str(artifact), "channel": "C1"})
            worker = OutboundWorker(store, slack, host)
            assert await worker.process_once()
            assert not any(path.endswith("completeUploadExternal") for path in paths)
            assert artifact.read_bytes() == b"immutable"
            assert store.delivery_receipt("file")["parts"][0]["state"] == "held"
            # The same file publication path proceeds when the place is Active.
            host.active = True
            clock[0] += 6
            def active_handler(request):
                response = handler(request)
                host.active = True
                return response
            http._transport = httpx.MockTransport(active_handler)
            assert await worker.process_once()
            assert sum(path.endswith("completeUploadExternal") for path in paths) == 1
            assert store.delivery_receipt("file")["parts"][0]["state"] == "delivered"
    asyncio.run(run())
