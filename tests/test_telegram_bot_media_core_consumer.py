"""telegram-bot 0.12.0 events → the real core ``/presence/turn`` (local opt-in; CI selects it).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_telegram_bot_media_core_consumer.py

Without that variable this is reported as skipped, never as passed. telegram-bot's parser, custody store,
runtime, Bot API client and Host client run unchanged; only Telegram's HTTP answers are scripted (synthetic
updates). Core's own Host Service app admits each event over ASGI from the skill's real state directory,
validates it, stages its files and builds the Presence task; a stand-in agent records what core handed it and
stays silent. Only skill-token authentication and the focus allowance are stubbed. Every run is a subprocess
whose HOME, data and settings are temporary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"
TOKEN = "fixture-skill-token"
BOT = "9"
ALBUM = "13981234567890123"
OLD_NOTE = "Not staged: larger than the Bot API's 20 MiB download limit. The Telegram message reference is retained."


def _run(tmp_path, mode):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; the core /presence/turn was not exercised")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": os.pathsep.join([str(Path(core).resolve()), str(ROOT / "skills" / "telegram-bot")]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run([sys.executable, __file__, str(tmp_path), mode], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_every_0_12_0_kind_passes_presence_turn_on_the_current_core(tmp_path):
    facts = _run(tmp_path, "new")
    turns = facts["turns"]
    assert facts["statuses"] == {f"telegram:{BOT}:{n}": 200 for n in range(1, 11)}
    assert facts["inbox_submitted"] == 10 and facts["inbox_failed"] == 0
    for source, turn in turns.items():
        # The top-level keys of each posted event are the frozen ones; everything new is inside message.
        assert facts["posted"][source] == sorted(["source_event_id", "provider", "account_id", "conversation_id",
                                                  "thread_id", "conversation_key", "actor", "conversation", "message",
                                                  "text", "facts"])
        assert turn["observed_text"] == facts["sent_text"][source]  # core received the exact words, none made up

    def turn(n):
        return turns[f"telegram:{BOT}:{n}"]

    # Video, round video and GIF arrive as files in core's manifest, captioned or not.
    assert [(r["status"], r["mime"]) for r in turn(1)["manifest"]] == [("staged", "video/mp4")]
    assert turn(1)["observed_text"] == "watch this"
    assert [r["status"] for r in turn(2)["manifest"]] == ["staged"]
    assert [r["label"] for r in turn(3)["manifest"]] == ["0-cat.gif.mp4"]  # once, as animation
    # Non-file kinds: described inside message, no text and no file.
    for n, kind in ((4, "sticker"), (5, "poll"), (6, "venue")):
        assert turn(n)["manifest"] == [] and turn(n)["observed_text"] == ""
        assert [entry["kind"] for entry in turn(n)["message"]["non_text"]] == [kind]
    # An album: the oversized zip is a too_large record (never a manifest row), the small one a staged file.
    large, small = turn(7), turn(8)
    assert large["manifest"] == [] and large["message"]["attachments"][0]["status"] == "too_large"
    assert large["message"]["attachments"][0]["note"] == "600 MB; the cloud Bot API gives bots files only up to 20 MB"
    assert [r["size"] for r in small["manifest"]] == [4_000_000] and small["observed_text"] == "исходники"
    assert [t["message"]["media_group_position"] for t in (large, small)] == [1, 2]
    assert {t["message"]["media_group_id"] for t in (large, small)} == {ALBUM}
    # Forward facts stay message facts; the forwarder is still the actor.
    forwarded = turn(9)
    assert forwarded["message"]["forward_origin"]["sender_user"]["id"] == 77 and forwarded["actor_id"] == "55"
    assert forwarded["message"]["is_automatic_forward"] is True
    # An unreadable file_id: a failed record that keeps its reference, no manifest row, the chat moved on.
    assert turn(10)["manifest"] == []
    assert {k: turn(10)["message"]["attachments"][0][k] for k in ("status", "file_id", "note")} == {
        "status": "failed", "file_id": "garbled", "note": 'Telegram answered "Bad Request: invalid file_id"'}


def test_a_0_10_6_shaped_event_still_passes_and_is_never_fetched(tmp_path):
    facts = _run(tmp_path, "old")
    assert facts["statuses"] == {f"telegram:{BOT}:1": 200, f"telegram:{BOT}:2": 200}
    old = facts["turns"][f"telegram:{BOT}:1"]
    assert old["manifest"] == [] and old["message"]["attachments"][0] == {
        "kind": "document", "file_id": "huge", "file_unique_id": "u-huge", "file_name": "archive.zip",
        "mime_type": "application/zip", "file_size": 600_000_000, "status": "too_large", "note": OLD_NOTE}
    assert [r["status"] for r in facts["turns"][f"telegram:{BOT}:2"]["manifest"]] == ["staged"]
    assert facts["get_file"] == ["small"]


# --------------------------------------------------------------------------- subprocess side


def _core(root):
    from types import SimpleNamespace

    import httpx

    from ouroboros import places as p, presence_admission, presence_context
    from ouroboros.gateway import host_service
    from ouroboros.presence_runner import run_presence_turn
    from ouroboros.task_results import write_task_result

    data, repo = root / "data", root / "repo"
    p.connect_place(data, connector="telegram-bot", provider="telegram", account_id=BOT, conversation_id="",
                    wakes=["any_message"])
    presence_admission.focus_enabled = lambda _root: True
    presence_admission.focus_allowance = lambda _root: {"status": "available", "remaining_usd": 2, "settled_usd": 1}
    presence_context._own_work_section = lambda *_args: ""

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token == TOKEN
        return "telegram-bot", {}
    host_service._authenticated = authenticated
    world = SimpleNamespace(data=data, statuses={}, turns={}, posted={})

    class Agent:
        """Records what core handed the turn, then stays silent."""

        def handle_task(self, task):
            metadata = task.get("metadata") or {}
            presence = metadata["presence"]
            contract = task.get("task_contract") or {}
            manifest = list(contract.get("attachment_manifest") or task.get("attachments") or [])
            event = presence["event"]
            world.turns[event["source_event_id"]] = {
                "observed_text": presence["observed_text"], "message": event.get("message") or {},
                "actor_id": (event.get("actor") or {}).get("platform_actor_id"),
                "manifest": [{key: row.get(key) for key in ("status", "label", "mime", "size")} for row in manifest],
            }
            write_task_result(data, task["id"], "completed", metadata=metadata, result="")
            return [{"type": "presence_result", "outcome": "silent", "text": "", "work_ref": ""}]

    def runner(**kwargs):
        return run_presence_turn(repo_dir=repo, drive_root=data, agent_factory=lambda **_: Agent(), **kwargs)

    app = host_service.create_host_service_app(data, presence_runner=runner)
    inner = httpx.ASGITransport(app=app)

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await inner.handle_async_request(request)
            if request.url.path == "/presence/turn":
                body = await response.aread()
                posted = json.loads(request.content)["event"]
                world.statuses[posted["source_event_id"]] = response.status_code
                world.posted[posted["source_event_id"]] = sorted(posted)
                return httpx.Response(response.status_code, content=body, headers=response.headers)
            return response
    world.transport = Transport()
    return world


def _bot(root, mode):
    import asyncio

    import httpx

    from ouroboros.skill_loader import skill_state_dir
    from telegram_bot.api import TelegramClient
    from telegram_bot.events import parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime

    world = _core(root)
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + bytes(64)
    files = {"vid": mp4, "note": mp4, "gif": mp4, "zip-small": bytes(4_000_000), "small": b"%PDF-1.4\n" + bytes(64)}
    get_file = []

    class Provider:
        async def post_json(self, url, payload, timeout_sec):
            assert url.endswith("/getFile"), url
            get_file.append(payload["file_id"])
            if payload["file_id"] not in files:
                return {"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}
            return {"ok": True, "result": {"file_id": payload["file_id"], "file_path": f"media/{payload['file_id']}"}}

        async def get_bytes(self, url, timeout_sec, max_bytes):
            return files[url.rsplit("/", 1)[-1]]

    class HostTransport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877",
                                         timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers,
                                                json=payload)
            body = response.json()
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = skill_state_dir(world.data, "telegram-bot")
    client = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                                transport=HostTransport())
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=client,
                                       client_factory=lambda token: TelegramClient(token, transport=Provider()))
    runtime._client, runtime._client_token, runtime._bot_id = TelegramClient("fake", transport=Provider()), "fake", BOT

    def update(n, **message):
        chat = {"id": -100200, "type": "supergroup", "title": "Test group"}
        return {"update_id": n, "message": {"message_id": 10 + n, "date": 1_700_000_000 + n, "chat": chat,
                                            "from": {"id": 55, "is_bot": False, "first_name": "Ann"}, **message}}

    def document(file_id, size, name):
        return {"file_id": file_id, "file_unique_id": f"u-{file_id}", "file_name": name,
                "mime_type": "application/zip" if name.endswith(".zip") else "application/pdf", "file_size": size}

    video = {"file_id": "vid", "file_unique_id": "uv", "duration": 3, "width": 64, "height": 64,
             "mime_type": "video/mp4", "file_name": "clip.mp4", "file_size": len(mp4)}
    if mode == "new":
        updates = [
            update(1, video=video, caption="watch this"),
            update(2, video_note={"file_id": "note", "file_unique_id": "un", "duration": 2, "length": 240,
                                  "file_size": len(mp4)}),
            update(3, animation={**video, "file_id": "gif", "file_unique_id": "ug", "file_name": "cat.gif.mp4"},
                   document={"file_id": "gif", "file_unique_id": "ug", "file_name": "cat.gif.mp4", "mime_type": "video/mp4"}),
            update(4, sticker={"file_id": "st", "file_unique_id": "us", "type": "regular", "width": 512, "height": 512,
                               "is_animated": False, "is_video": False, "emoji": "👍", "set_name": "Thumbs"}),
            update(5, poll={"id": "p", "question": "Lunch?", "type": "regular", "is_anonymous": False,
                            "is_closed": False, "allows_multiple_answers": False, "total_voter_count": 0,
                            "options": [{"text": "Pizza", "voter_count": 0}, {"text": "Soup", "voter_count": 0}]}),
            update(6, venue={"location": {"latitude": 55.75, "longitude": 37.62}, "title": "Cafe", "address": "Main 1"},
                   location={"latitude": 55.75, "longitude": 37.62}),
            update(7, media_group_id=ALBUM, document=document("zip-big", 600_000_000, "archive-full.zip")),
            update(8, media_group_id=ALBUM, caption="исходники", document=document("zip-small", 4_000_000, "archive-part.zip")),
            update(9, text="forwarded words", is_automatic_forward=True, forward_origin={
                "type": "user", "date": 1_699_000_000, "sender_user": {"id": 77, "is_bot": False, "first_name": "Bob"}}),
            update(10, document=document("garbled", 900, "notes.pdf")),
        ]
        events = [parse_telegram_update(item, bot_account_id=BOT, places=True) for item in updates]
    else:
        old = parse_telegram_update(update(1, document=document("huge", 600_000_000, "archive.zip")), bot_account_id=BOT,
                                    places=True)
        old.message["attachments"][0]["note"] = OLD_NOTE  # exactly as 0.10.6 stored it
        newer = parse_telegram_update(update(2, document=document("small", len(files["small"]), "notes.pdf")),
                                      bot_account_id=BOT, places=True)
        events = [old, newer]

    async def work():
        await client.discover_delivery_support()
        for number, event in enumerate(events, start=1):
            runtime.store.commit_update(number, event)
        for _ in events:
            assert await runtime.process_one_inbox()
    asyncio.run(work())
    snapshot = runtime.store.status_snapshot()
    return {"statuses": world.statuses, "turns": world.turns, "get_file": get_file, "posted": world.posted,
            "sent_text": {event.source_event_id: event.text for event in events},
            "inbox_submitted": snapshot["inbox_submitted"], "inbox_failed": snapshot["inbox_failed"]}


if __name__ == "__main__":
    print(json.dumps(_bot(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, default=str))
