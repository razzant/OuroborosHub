"""Telegram voice/audio → real core Presence staging → Whisper's attachment tool (local opt-in).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_voice_attachment_core_consumer.py

Without that variable this is reported as skipped, never as passed. Both Telegram connectors run
unchanged: telegram-bot's parser, custody store, runtime and Bot API client (only Telegram's HTTP
answers are scripted), and telegram-user-session's Participant capture and intake (only Telethon's
client is a fake). Their staged files reach core's own ``/presence/turn`` app over ASGI, from each
skill's real state directory. Core then confines, stages and classifies the bytes, builds the
Presence task and calls the agent factory with it; the stand-in agent prepares a ToolContext the
way core's agent does, sends it through the extension child's own context payload round trip and
calls whisper-voice's ``transcribe_attachment`` with each manifest ordinal. Core's resource binding
and media admission run for real. Only skill-token authentication, the focus allowance and the
Whisper companion are stubbed: the worker is a fake that records what it was handed and answers
with the companion's documented replies. No model weights, inference, network or Telegram.
Every run is a subprocess whose HOME, data and settings are temporary.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"
TOKEN = "fixture-skill-token"
BOT, ACCOUNT = "9", "8667525238"
KIB = 1024
# Real container heads: Ogg page, bare MPEG frame sync (no ID3 tag), FLAC.
OGG = b"OggS\x00\x02" + bytes(40) + b"opus-voice"
MP3 = b"\xff\xfb\x90\x64" + bytes(60) + b"mpeg-frames"
FLAC = b"fLaC\x00\x00\x00\x22" + bytes(40)
# An M4A as iOS and many recorders write it: generic ``isom`` major brand, no ``M4A `` brand.
M4A = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + bytes(32) + b"aac-frames"
LONG_OGG = b"OggS\x00\x02" + bytes(400 * KIB)  # larger than Whisper's 384 KiB, far below Telegram's limits


def _run(tmp_path, mode):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; the core Presence staging was not exercised")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": os.pathsep.join([str(Path(core).resolve()), str(ROOT / "skills" / "telegram-bot"),
                                       str(ROOT / "skills" / "telegram-user-session")]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run([sys.executable, __file__, str(tmp_path), mode], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_bot_voice_and_audio_reach_whisper_as_their_original_bytes(tmp_path):
    facts = _run(tmp_path, "bot")
    assert facts["statuses"] == {f"telegram:{BOT}:{n}": 200 for n in (1, 2, 3, 4, 5)}
    assert facts["downloads"] == ["voice-1", "audio-2", "voice-3", "audio-4"]  # never the 20 MiB+ one
    for source, turn in facts["turns"].items():
        assert turn["identity"] == facts["posted"][source]  # author, chat and source unchanged by core
        assert turn["identity"]["platform_actor_id"] == "55" and turn["identity"]["account_id"] == BOT

    voice = facts["turns"][f"telegram:{BOT}:1"]
    [row] = voice["manifest"]
    assert (row["status"], row["mime"], row["label"]) == ("staged", "audio/ogg", "0-voice-u1.ogg")
    assert (row["sha256"], row["size"]) == (_sha(OGG), len(OGG))
    assert "[ATTACHMENTS]" in voice["text"] and "(audio/ogg)" in voice["text"] and "ordinal=0" in voice["text"]
    assert voice["observed_text"] == "" and voice["facts"] == ["any_message", "direct_message"]
    assert voice["context_attachments"][0]["kind"] == "voice" and voice["context_attachments"][0]["duration_sec"] == 3
    # Whisper's tool read the staged file through core's guards and handed the worker the exact bytes.
    assert voice["worker_calls"] == [["POST", "/transcribe", _sha(OGG), "audio/ogg"]] * 4
    ok, unavailable, overlong, silent = voice["tool"]
    assert ok == {"ok": True, "text": "fake local words", "timings": {"model_loaded": False}}
    assert unavailable["ok"] is False and unavailable["error"]["code"] == "worker_unavailable" and "text" not in unavailable
    assert overlong["ok"] is False and overlong["error"]["code"] == "voice_unavailable" and "text" not in overlong
    assert silent == {"ok": True, "text": "", "status": "no_speech", "timings": {}}
    assert voice["missing_ordinal"]["ok"] is False and "text" not in voice["missing_ordinal"]

    # An MP3 with no ID3 tag is audio to core only under its suffix, which the bot now supplies.
    mp3 = facts["turns"][f"telegram:{BOT}:2"]
    # The display label is capped separately; the real staged path retains its suffix.
    assert [(r["label"], r["mime"]) for r in mp3["manifest"]] == [("0-" + "a" * 118, "audio/mpeg")]
    assert mp3["manifest"][0]["relpath"].endswith(".mp3")
    assert mp3["tool"][0]["ok"] is True and mp3["worker_calls"][0][2:] == [_sha(MP3), "audio/mpeg"]
    assert facts["unsuffixed_mp3_mime"] == "application/octet-stream"  # what 0.10.0's name would have meant

    # Larger than Whisper takes: staged whole (never cut), refused by the tool, no words.
    long = facts["turns"][f"telegram:{BOT}:3"]
    [row] = long["manifest"]
    assert (row["mime"], row["size"], row["sha256"]) == ("audio/ogg", len(LONG_OGG), _sha(LONG_OGG))
    assert long["tool"][0]["ok"] is False and long["tool"][0]["error"]["code"] == "attachment_blocked"
    assert "FILE_TOO_LARGE" in long["tool"][0]["error"]["message"] and long["worker_calls"] == []

    # A format Whisper does not take stays a file.
    flac = facts["turns"][f"telegram:{BOT}:4"]
    assert [(r["label"], r["mime"]) for r in flac["manifest"]] == [("0-tune.flac", "audio/flac")]
    assert flac["tool"][0]["ok"] is False and "unsupported audio type" in flac["tool"][0]["error"]["message"]
    assert flac["worker_calls"] == []

    # Above the Bot API's download limit: nothing staged, the reason is in the event, nothing to transcribe.
    big = facts["turns"][f"telegram:{BOT}:5"]
    assert big["manifest"] == [] and big["context_attachments"][0]["status"] == "too_large"
    assert big["missing_ordinal"]["ok"] is False and big["worker_calls"] == []
    assert facts["inbox_submitted"] == 5


def test_user_session_voice_and_m4a_reach_whisper_as_their_original_bytes(tmp_path):
    facts = _run(tmp_path, "user-session")
    assert sorted(facts["statuses"].values()) == [200, 200]
    turns = {turn["identity"]["platform_actor_id"]: turn for turn in facts["turns"].values()}
    assert set(turns) == {"809", "810"}
    for source, turn in facts["turns"].items():
        # Core handed the agent the author, chat and source event the connector posted.
        assert turn["identity"] == facts["posted"][source]
        assert turn["identity"]["account_id"] == ACCOUNT and turn["facts"] == ["any_message", "direct_message"]

    voice = turns["809"]
    [row] = voice["manifest"]
    assert row["status"] == "staged" and row["mime"] == "audio/ogg" and row["label"].endswith(".ogg")
    assert (row["sha256"], row["size"]) == (_sha(OGG), len(OGG))
    assert voice["identity"]["conversation_id"] == "809"
    assert voice["observed_text"] == "[Telegram voice message]"  # a label, not a transcript
    assert voice["context_media"]["kind"] == "voice" and voice["context_media"]["status"] == "staged"
    assert voice["worker_calls"] == [["POST", "/transcribe", _sha(OGG), "audio/ogg"]]
    assert voice["tool"] == [{"ok": True, "text": "fake local words", "timings": {"model_loaded": False}}]

    # The actual 0.4.0 bug: audio/x-m4a with no file name and a generic MP4 brand. Staged as .bin
    # core calls it video/mp4 and Whisper refuses it; staged as .m4a it is audio/mp4.
    m4a = turns["810"]
    [row] = m4a["manifest"]
    assert (row["status"], row["mime"]) == ("staged", "audio/mp4") and row["label"].endswith(".m4a"), row
    assert (row["sha256"], row["size"]) == (_sha(M4A), len(M4A))
    assert m4a["identity"]["conversation_id"] == "810"
    assert m4a["observed_text"] == "[Telegram audio]"
    assert (m4a["context_media"]["kind"], m4a["context_media"]["mime"], m4a["context_media"]["status"]) == (
        "audio", "audio/x-m4a", "staged")
    assert "name" not in m4a["context_media"]
    assert m4a["worker_calls"] == [["POST", "/transcribe", _sha(M4A), "audio/mp4"]]
    assert m4a["tool"] == [{"ok": True, "text": "fake local words", "timings": {"model_loaded": False}}]
    assert facts["downloads"] == ["77", "78"]


# --------------------------------------------------------------------------- subprocess side


WORKER_REPLIES = (
    (200, {"text": "fake local words", "timings": {"model_loaded": False}}),
    (503, {"error": {"code": "worker_unavailable", "message": "Voice worker unavailable: ConnectionRefusedError"}}),
    (400, {"error": {"code": "voice_unavailable", "message": "audio longer than 60 seconds"}}),
    (200, {"text": "", "timings": {}, "status": "no_speech"}),
)


def _identity(event):
    """Author and source facts that must leave core exactly as the connector sent them."""
    actor = event.get("actor") or {}
    return {"source_event_id": event.get("source_event_id"), "provider": event.get("provider"),
            "account_id": event.get("account_id"), "conversation_id": event.get("conversation_id"),
            "thread_id": event.get("thread_id"), "conversation_key": event.get("conversation_key"),
            "platform_actor_id": actor.get("platform_actor_id")}


def _whisper():
    """whisper-voice's own attachment tool, imported as its package would be."""
    import importlib
    import types

    package = types.ModuleType("_whisper_voice")
    package.__path__ = [str(ROOT / "skills" / "whisper-voice")]
    sys.modules[package.__name__] = package
    return importlib.import_module("_whisper_voice.attachment_tool")


def _core(root, connector, account):
    from types import SimpleNamespace

    import httpx

    from ouroboros import places as p, presence_admission, presence_context
    from ouroboros.consciousness_authority import apply_consciousness_authority
    from ouroboros.contracts.task_contract import attach_task_contract
    from ouroboros.extension_process_runner import _apply_tool_context_payload, _tool_context_payload
    from ouroboros.gateway import host_service
    from ouroboros.presence_runner import run_presence_turn
    from ouroboros.task_results import write_task_result
    from ouroboros.tool_access_roots import freeze_tool_profile
    from ouroboros.tools.tool_context import ToolContext
    from starlette.responses import JSONResponse

    data, repo = root / "data", root / "repo"
    p.connect_place(data, connector=connector, provider="telegram", account_id=account, conversation_id="",
                    wakes=["any_message"])
    presence_admission.focus_enabled = lambda _root: True
    presence_admission.focus_allowance = lambda _root: {"status": "available", "remaining_usd": 2, "settled_usd": 1}
    presence_context._own_work_section = lambda *_args: ""

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token == TOKEN
        return connector, {}
    host_service._authenticated = authenticated
    adapter = _whisper()
    world = SimpleNamespace(data=data, statuses={}, turns={}, posted={})

    class Agent:
        """Prepares the tool context as core's agent does, then only calls Whisper's tool."""

        def handle_task(self, task):
            task = attach_task_contract(apply_consciousness_authority(task))
            freeze_tool_profile(task)
            metadata = task.get("metadata") or {}
            parent = ToolContext(
                repo_dir=repo, drive_root=data, task_id=str(task["id"]), task_metadata=metadata,
                workspace_root=Path(task["workspace_root"]) if task.get("workspace_root") else None,
                workspace_mode=str(task.get("workspace_mode") or ""), memory_mode=str(task.get("memory_mode") or ""),
                current_task_type=task.get("type"), task_contract=task["task_contract"])
            # The extension child receives exactly this payload.
            ctx = _apply_tool_context_payload(ToolContext(repo_dir=repo, drive_root=data), _tool_context_payload(parent))
            presence = metadata["presence"]
            calls, tool = [], []

            def worker_for(reply):
                def worker(_state, method, path, body, mime):
                    calls.append([method, path, hashlib.sha256(body).hexdigest(), mime])
                    return JSONResponse(reply[1], status_code=reply[0])
                return worker
            manifest = list(task["task_contract"].get("attachment_manifest") or [])
            for row in manifest:
                replies = WORKER_REPLIES if row["label"].startswith("0-voice-u1") else WORKER_REPLIES[:1]
                for reply in replies:
                    tool.append(json.loads(adapter.transcribe_attachment(
                        ctx, row["ordinal"], state_dir=root, worker=worker_for(reply))))
            message = presence["event"].get("message") or {}
            world.turns[presence["event"]["source_event_id"]] = {
                "text": task["text"], "observed_text": presence["observed_text"],
                "facts": presence["event"].get("facts"),
                "manifest": [{key: row.get(key) for key in ("status", "label", "mime", "sha256", "size", "ordinal", "relpath")}
                             for row in manifest],
                "context_attachments": message.get("attachments") or [], "context_media": message.get("media"),
                "tool": tool, "worker_calls": calls, "identity": _identity(presence["event"]),
                "missing_ordinal": json.loads(adapter.transcribe_attachment(ctx, len(manifest), state_dir=root,
                                                                            worker=worker_for(WORKER_REPLIES[0]))),
            }
            write_task_result(data, task["id"], "completed", metadata=task["metadata"], result="")
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
                world.posted[posted["source_event_id"]] = _identity(posted)
                return httpx.Response(response.status_code, content=body, headers=response.headers)
            return response
    world.transport = Transport()
    return world


def _bot(root):
    import asyncio

    import httpx

    from ouroboros.chat_uploads import detect_media
    from ouroboros.skill_loader import skill_state_dir
    from telegram_bot.api import TelegramClient
    from telegram_bot.events import BOT_API_DOWNLOAD_LIMIT, parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime

    world = _core(root, "telegram-bot", BOT)
    files = {"voice-1": OGG, "audio-2": MP3, "voice-3": LONG_OGG, "audio-4": FLAC}
    downloads = []

    class Provider:
        async def post_json(self, url, payload, timeout_sec):
            assert url.endswith("/getFile"), url
            return {"ok": True, "result": {"file_id": payload["file_id"], "file_path": f"media/{payload['file_id']}"}}

        async def get_bytes(self, url, timeout_sec, max_bytes):
            name = url.rsplit("/", 1)[-1]
            downloads.append(name)
            return files[name]

    class HostTransport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
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

    def update(n, **media):
        chat = {"id": 100 + n, "type": "private", "first_name": "Ann"}
        return {"update_id": n, "message": {"message_id": 10 + n, "date": 1_700_000_000 + n, "chat": chat,
                                            "from": {"id": 55, "is_bot": False, "first_name": "Ann"}, **media}}
    updates = [
        update(1, voice={"file_id": "voice-1", "file_unique_id": "u1", "duration": 3, "mime_type": "audio/ogg",
                         "file_size": len(OGG)}),
        update(2, audio={"file_id": "audio-2", "file_unique_id": "u2", "duration": 5, "mime_type": "audio/mpeg",
                         "file_name": "a" * 180 + ".mp3", "file_size": len(MP3)}),
        update(3, voice={"file_id": "voice-3", "file_unique_id": "u3", "duration": 200, "mime_type": "audio/ogg",
                         "file_size": len(LONG_OGG)}),
        update(4, audio={"file_id": "audio-4", "file_unique_id": "u4", "duration": 5, "mime_type": "audio/flac",
                         "file_name": "tune.flac", "file_size": len(FLAC)}),
        update(5, voice={"file_id": "voice-5", "file_unique_id": "u5", "duration": 4000, "mime_type": "audio/ogg",
                         "file_size": BOT_API_DOWNLOAD_LIMIT + 1}),
    ]

    async def work():
        await client.discover_delivery_support()
        for item in updates:
            runtime.store.commit_update(item["update_id"], parse_telegram_update(item, bot_account_id=BOT, places=True))
        for _ in range(len(updates)):
            assert await runtime.process_one_inbox()
    asyncio.run(work())
    return {"statuses": world.statuses, "turns": world.turns, "posted": world.posted, "downloads": downloads,
            "inbox_submitted": runtime.store.status_snapshot()["inbox_submitted"],
            "unsuffixed_mp3_mime": detect_media(MP3, "0-recording")[0]}


def _user_session(root):
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS

    import httpx
    from telethon import types as T

    from ouroboros.skill_loader import skill_state_dir
    from userbot.config import Config, save_config
    from userbot.participant import Host, Participant
    from userbot.participant_state import Journal, save_policy
    from userbot.telegram import TelegramAdapter

    world = _core(root, "telegram-user-session", ACCOUNT)
    state = skill_state_dir(world.data, "telegram-user-session")
    save_config(state, Config(123, "a" * 32, "+1234567890", account_id=int(ACCOUNT)))
    save_policy(state, {"mode": "participant", "outgoing": False, "expected_account_id": ACCOUNT})
    now = datetime.now(timezone.utc)
    served = {77: OGG, 78: M4A}  # Telegram's bytes for each document, exactly as sent
    downloads = []
    people = {809: "Ann", 810: "Bob"}

    class Client:
        async def get_entity(self, peer):
            return T.User(id=int(peer), first_name=people[int(peer)], access_hash=1)

        async def get_messages(self, *args, **kwargs):
            return None

        async def download_media(self, message, file):
            downloads.append(str(message.media.document.id))
            Path(file).write_bytes(served[message.media.document.id])

    class Adapter(TelegramAdapter):
        async def topic_ids(self, peer):
            return set()

    def incoming(peer, mid, doc_id, mime, attributes):
        doc = T.Document(id=doc_id, access_hash=9, file_reference=b"f", date=now, mime_type=mime,
                         size=len(served[doc_id]), dc_id=1, attributes=attributes)
        message = T.Message(id=mid, peer_id=T.PeerUser(peer), date=now, message="", from_id=T.PeerUser(peer),
                            media=T.MessageMediaDocument(document=doc))
        return NS(message=message, chat_id=peer, sender_id=peer, raw_text="", is_private=True,
                  get_sender=lambda: client.get_entity(peer), get_chat=lambda: client.get_entity(peer))

    client = Client()

    async def work():
        loop = asyncio.get_running_loop()

        def transport(method, path, payload, headers):
            async def send():
                async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877",
                                             timeout=60.0) as http:
                    response = await http.request(method, path, headers=headers, json=payload)
                return response.status_code, response.json()
            return asyncio.run_coroutine_threadsafe(send(), loop).result(120)

        host = Host("http://127.0.0.1:18877", NS(use_in_request=lambda: TOKEN), transport=transport)
        journal = Journal(state)
        engine = Participant(state, Adapter(state / "unused.session", 123, "a" * 32, client=client), host, journal,
                             int(ACCOUNT))
        await engine.capture(incoming(809, 10, 77, "audio/ogg", [T.DocumentAttributeAudio(duration=3, voice=True)]))
        # No DocumentAttributeFilename: Telegram sent no name, so only the declared type can give a suffix.
        await engine.capture(incoming(810, 11, 78, "audio/x-m4a", [T.DocumentAttributeAudio(duration=3, voice=False)]))
        for _ in range(4):
            await engine.submit_once()
            if len(world.statuses) == 2:
                break
        journal.close()
    asyncio.run(work())
    return {"statuses": world.statuses, "turns": world.turns, "posted": world.posted, "downloads": downloads}


if __name__ == "__main__":
    mode = sys.argv[2]
    result = _bot(Path(sys.argv[1])) if mode == "bot" else _user_session(Path(sys.argv[1]))
    print(json.dumps(result, ensure_ascii=False, default=str))
