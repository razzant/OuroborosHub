"""Bot API Presence contribution: real private store, fake network boundary."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[1]
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from bot.config import empty, save  # noqa: E402
from bot.runtime import identity, run  # noqa: E402
from bot.store import Store  # noqa: E402

PHOTO = b"\xff\xd8\xff\xdb\x00"  # actual JPEG signature for byte-sniffing Host


def _update(uid, chat=-1001, thread=42, *, photo=True):
    message = {"message_id": uid + 10, "chat": {"id": chat, "is_forum": True},
               "message_thread_id": thread, "from": {"id": uid + 100}}
    if photo:
        message["photo"] = [{"file_id": f"photo-{uid}", "file_size": 5}]
    else:
        message["text"] = "hello"
    return {"update_id": uid, "message": message}


class _Bot:
    def __init__(self, updates):
        self.updates, self.calls = updates, []

    def api(self, method, payload, **kwargs):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"is_bot": True, "id": 777}
        if method == "getUpdates":
            return self.updates
        if method == "sendMessage":
            if not kwargs["pre_send"]():
                raise RuntimeError("owner binding revoked")
            return {"message_id": 920, "message_thread_id": payload.get("message_thread_id")}
        raise AssertionError(method)

    def download(self, file_id, target, *, byte_limit):
        assert file_id.startswith("photo-") and byte_limit >= 5
        Path(target).write_bytes(PHOTO)
        import hashlib
        return 5, hashlib.sha256(PHOTO).hexdigest()


class _Host:
    def __init__(self):
        self.turns, self.reports = [], []

    def identity(self):
        return None

    def turn(self, binding, event, files):
        self.turns.append((binding, event, files))
        assert files and Path(files[0]).read_bytes() == PHOTO
        return {"ok": True, "status": "completed", "outcome": "message", "text": "Hello, forum"}

    def work(self, *_):
        raise AssertionError("no deferred work")

    def delivered(self, **kwargs):
        self.reports.append(kwargs)
        return True


def _cfg(root, *, topic=42):
    config = empty()
    config["bindings"] = [{"chat_id": -1001, "topic_id": topic, "binding_id": "a" * 32}]
    save(root, config)


def test_unbound_private_media_and_bound_forum_from_same_polled_batch(tmp_path):
    _cfg(tmp_path)
    bot = _Bot([_update(1, chat=-1002), _update(2)])
    host = _Host()
    run(tmp_path, bot=bot, host=host, max_polls=1)
    assert len(host.turns) == 1 and host.turns[0][1]["thread_id"] == "42"
    assert host.turns[0][1]["conversation_id"] == "-1001"
    assert bot.calls[-1][0] == "sendMessage"
    assert bot.calls[-1][1]["message_thread_id"] == 42
    assert host.reports and host.reports[0]["chat"] == -1001
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.offset() == 3
        assert store.counts() == {"held": 1, "done": 1}
    assert (tmp_path / "media" / "1.jpg").read_bytes() == PHOTO
    # Test the real attachment classifier, not merely our fake Host's byte
    # read: an extensionless .bin would silently lose native image input.
    from ouroboros.artifacts import stage_task_attachments
    drive = tmp_path / "host-data"
    drive.mkdir()
    attached = stage_task_attachments(drive, "photo-task", [
        {"path": str(tmp_path / "media" / "1.jpg")},
    ])
    assert attached[0]["is_image"] is True
    assert attached[0]["mime"] == "image/jpeg"


def test_general_binding_cannot_take_an_unmapped_topic(tmp_path):
    _cfg(tmp_path, topic=None)
    bot = _Bot([_update(1)])
    host = _Host()
    run(tmp_path, bot=bot, host=host, max_polls=1)
    assert not host.turns and not host.reports
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}


def test_no_guessing_general_or_actor_for_incomplete_forum_message():
    msg = {"message_id": 12, "chat": {"id": -1001, "is_forum": True}, "from": {"id": 123}}
    assert identity(msg, bot_id=777) is None
    msg["message_thread_id"] = 42
    msg.pop("from")
    assert identity(msg, bot_id=777) is None


def test_offset_advanced_only_with_durable_update_and_unknown_send_never_requeued(tmp_path):
    with Store(tmp_path / "private.sqlite3") as store:
        assert store.capture(_update(11, photo=False)) == "pending"
        assert store.offset() == 12
        source = "telegram:777:-1001:42:21:message:11"
        store.queue_reply(source, -1001, 42, 21, "a" * 32, "answer")
        asserted = store.claim_reply()
        assert asserted["source_id"] == source
        assert store.claim_reply() is None
    # The exclusive companion conservatively retires a crashed attempted send.
    with Store(tmp_path / "private.sqlite3", recover_attempted=True) as store:
        assert store.claim_reply() is None
        assert store.db.execute("SELECT status FROM outbound").fetchone()[0] == "unknown"
        assert store.capture(_update(11, photo=False)) == "pending"
        assert store.offset() == 12
        with pytest.raises(ValueError, match="identity changed"):
            store.capture(dict(_update(11, photo=False), message={"text": "different"}))


def test_unrenderable_media_is_held_not_an_empty_presence_turn(tmp_path):
    _cfg(tmp_path)
    update = _update(4, photo=False)
    update["message"].pop("text")
    update["message"]["paid_media"] = [{"type": "photo"}]
    host = _Host()
    run(tmp_path, bot=_Bot([update]), host=host, max_polls=1)
    assert not host.turns
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}


def test_real_host_presence_admits_bound_photo_as_image(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from ouroboros.artifacts import stage_task_attachments
    from ouroboros.gateway import host_service
    from ouroboros.gateway.host_service import create_host_service_app
    from ouroboros.presence_bindings import (
        PresenceBinding,
        PresenceEndpoint,
        new_presence_binding_id,
        save_presence_binding,
    )
    from tests.test_host_service_api import _seed_presence_behavior, _seed_token

    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", "")
    _seed_token(tmp_path, skill="telegram-presence-bot", token="host-private",
                permissions=["presence"], manifest_permissions=["presence"])
    _seed_presence_behavior(tmp_path)
    endpoint = PresenceEndpoint("telegram", "777", "-1001", "42")
    binding = save_presence_binding(tmp_path, PresenceBinding(
        new_presence_binding_id(), "telegram-presence-bot", "community-helper",
        endpoint, endpoint,
    ))
    state = tmp_path / "state" / "skills" / "telegram-presence-bot" / "media"
    state.mkdir(parents=True, exist_ok=True)
    photo = state / "1.jpg"
    photo.write_bytes(PHOTO)
    observed = []

    def runner(**kwargs):
        paths = kwargs["staged_files"]
        observed.append(stage_task_attachments(tmp_path, "host-photo", [{"path": str(paths[0])}]))
        return SimpleNamespace(outcome="silent", text="", task_id="host-photo", work_ref="")

    app = create_host_service_app(tmp_path, presence_runner=runner)
    event = {"source_event_id": "telegram:777:-1001:42:12:message:1", "provider": "telegram",
             "account_id": "777", "conversation_id": "-1001", "thread_id": "42",
             "conversation_key": "ignored-by-host", "actor": {"platform_actor_id": "9"},
             "conversation": {}, "message": {"message_id": "12", "media": {"kind": "photo", "status": "ready"}},
             "text": "[Telegram photo media: ready]"}

    async def body():
        return {"binding_id": binding.binding_id, "event": event, "staged_files": [str(photo)]}

    request = SimpleNamespace(app=app, headers={"x-skill-token": "host-private"}, json=body)
    response = asyncio.run(host_service._api_presence_turn(request))
    assert response.status_code == 200, response.body
    assert observed and observed[0][0]["is_image"] is True
    assert observed[0][0]["mime"] == "image/jpeg"
    # The General thread is a different owner decision, never the same bind.
    event["thread_id"] = ""
    response = asyncio.run(host_service._api_presence_turn(request))
    assert response.status_code == 403


def test_mp3_store_reaches_real_host_presence_as_mpeg(tmp_path, monkeypatch):
    import asyncio
    import hashlib
    from types import SimpleNamespace

    from bot.host import HostClient
    from bot.media import MediaStore, describe

    from ouroboros.artifacts import stage_task_attachments
    from ouroboros.gateway import host_service
    from ouroboros.gateway.host_service import create_host_service_app
    from ouroboros.presence_bindings import (
        PresenceBinding,
        PresenceEndpoint,
        new_presence_binding_id,
        save_presence_binding,
    )
    from tests.test_host_service_api import _seed_presence_behavior, _seed_token

    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", "")
    _seed_token(tmp_path, skill="telegram-presence-bot", token="host-private",
                permissions=["presence"], manifest_permissions=["presence"])
    _seed_presence_behavior(tmp_path)
    endpoint = PresenceEndpoint("telegram", "777", "-1001", "42")
    binding = save_presence_binding(tmp_path, PresenceBinding(
        new_presence_binding_id(), "telegram-presence-bot", "community-helper", endpoint, endpoint))
    media = MediaStore(tmp_path / "state" / "skills" / "telegram-presence-bot")
    data = b"ID3fake-audio"
    descriptor = describe({"audio": {"file_id": "mp3-id", "file_size": len(data),
                                   "mime_type": "audio/mpeg"}})

    class Downloader:
        def download(self, file_id, target, *, byte_limit):
            assert file_id == "mp3-id" and byte_limit >= len(data)
            Path(target).write_bytes(data)
            return len(data), hashlib.sha256(data).hexdigest()

    assert media.capture(1, descriptor, empty(), Downloader())["status"] == "ready"
    source, receipt = media.ready_file(1)
    assert source.suffix == ".mp3" and receipt["suffix"] == ".mp3"
    observed = []

    def runner(**kwargs):
        observed.append(stage_task_attachments(tmp_path, "host-mp3", [
            {"path": kwargs["staged_files"][0]}]))
        return SimpleNamespace(outcome="silent", text="", task_id="host-mp3", work_ref="")

    app = create_host_service_app(tmp_path, presence_runner=runner)
    event = {"source_event_id": "telegram:777:-1001:42:12:message:1", "provider": "telegram",
             "account_id": "777", "conversation_id": "-1001", "thread_id": "42",
             "conversation_key": "ignored", "actor": {"platform_actor_id": "9"},
             "conversation": {}, "message": {"message_id": "12", "media": {"kind": "audio", "status": "ready"}},
             "text": "[Telegram audio media: ready]"}

    async def body():
        return {"binding_id": binding.binding_id, "event": event, "staged_files": [str(source)]}

    request = SimpleNamespace(app=app, headers={"x-skill-token": "host-private"}, json=body)
    response = asyncio.run(host_service._api_presence_turn(request))
    assert response.status_code == 200, response.body
    assert observed[0][0]["mime"] == "audio/mpeg" and observed[0][0]["abs_path"].endswith(".mp3")
    # MP3 remains the original typed attachment, and an available local
    # transcriber receives the exact MIME rather than false Ogg metadata.
    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b'{"text":"heard mp3"}'

    class Opener:
        def open(self, request, *, timeout):
            assert request.get_header("Content-type") == "audio/mpeg"
            assert request.data == data and timeout == 120
            return Reply()

    assert HostClient("http://127.0.0.1:8767", "host-private", opener=Opener()).transcribe(
        binding.binding_id, 777, -1001, 42, data, receipt["mime"]) == ("heard mp3", "ready")


def test_bot_audio_mime_unknown_and_legacy_receipt(tmp_path):
    import hashlib
    import json

    from bot.media import MediaStore, describe

    class Downloader:
        def download(self, _fid, target, *, byte_limit):
            Path(target).write_bytes(b"mp3")
            return 3, hashlib.sha256(b"mp3").hexdigest()

    media = MediaStore(tmp_path)
    descriptor = describe({"audio": {"file_id": "a", "file_size": 3, "mime_type": "audio/mpeg"}})
    receipt = media.capture(1, descriptor, empty(), Downloader())
    assert receipt["suffix"] == ".mp3" and media._used() == 3
    source, _ = media.ready_file(1)
    assert source.read_bytes() == b"mp3"
    source.rename(media.root / "1.ogg")  # pre-fix writer published Ogg name
    receipt_file = media.root / "1.json"
    receipt_file.write_text(json.dumps({k: v for k, v in receipt.items() if k != "suffix"}))
    with pytest.raises(ValueError, match="legacy audio suffix ambiguous"):
        media.ready_file(1)
    assert (media.root / "1.ogg").read_bytes() == b"mp3"
    unknown = describe({"audio": {"file_id": "b", "file_size": 3}})
    assert media.capture(2, unknown, empty(), Downloader())["suffix"] == ".bin"
    assert media.ready_file(2)[0].suffix == ".bin" and media._used() == 6


def test_bot_parameterized_audio_mime_is_normalized_before_transcription():
    from bot.media import describe

    assert describe({"audio": {"file_id": "a", "mime_type": "audio/ogg; codecs=opus"}})["mime"] == "audio/ogg"
    assert describe({"audio": {"file_id": "b", "mime_type": "audio/webm;codecs=opus"}})["mime"] == "audio/webm"
    assert describe({"audio": {"file_id": "c"}})["mime"] == "application/octet-stream"


def test_old_parameterized_ogg_receipt_transcribes_as_ogg(tmp_path):
    import hashlib
    import json

    from bot.host import HostClient
    from bot.media import MediaStore

    store = MediaStore(tmp_path)
    (store.root / "1.ogg").write_bytes(b"OggS-old-audio")
    receipt = {"update_id": 1, "kind": "audio", "file_id": "old-audio",
               "mime": "audio/ogg;codecs=opus", "suffix": ".ogg", "status": "ready",
               "bytes": 14, "sha256": hashlib.sha256(b"OggS-old-audio").hexdigest()}
    (store.root / "1.json").write_text(json.dumps(receipt))
    source, saved = store.ready_file(1)
    sent = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b'{"text":"heard"}'

    class Opener:
        def open(self, req, *, timeout):
            sent.append((req.get_header("Content-type"), req.data, timeout))
            return Reply()

    host = HostClient("http://127.0.0.1:8767", "private", opener=Opener())
    assert host.transcribe("a" * 32, 777, -1001, 42, source.read_bytes(), saved["mime"]) == ("heard", "ready")
    assert sent == [("audio/ogg", b"OggS-old-audio", 120)]
    assert saved["mime"] == "audio/ogg;codecs=opus"  # receipt was not rewritten


def test_old_unsupported_transcription_holds_ambiguous_bot_turn(tmp_path):
    import hashlib
    import json

    from bot.media import MediaStore

    _cfg(tmp_path)
    event = _update(1, photo=False)
    event["message"].pop("text")
    event["message"]["audio"] = {"file_id": "old-ogg", "file_size": 14,
                                 "mime_type": "audio/ogg;codecs=opus"}
    media = MediaStore(tmp_path)
    (media.root / "1.ogg").write_bytes(b"OggS-old-audio")
    (media.root / "1.json").write_text(json.dumps({
        "update_id": 1, "kind": "audio", "file_id": "old-ogg", "mime": "audio/ogg;codecs=opus",
        "suffix": ".ogg", "status": "ready", "bytes": 14,
        "sha256": hashlib.sha256(b"OggS-old-audio").hexdigest(),
    }))
    with Store(tmp_path / "transport.sqlite3") as store:
        store.capture(event, binding_id="a" * 32)
        store.freeze_transcript(1, {"status": "audio_unsupported", "text": ""})
    host = _Host()
    run(tmp_path, bot=_Bot([event]), host=host, max_polls=1)
    run(tmp_path, bot=_Bot([event]), host=host, max_polls=1)
    assert not host.turns and (media.root / "1.ogg").read_bytes() == b"OggS-old-audio"
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}
        transcript = json.loads(store.db.execute("SELECT transcript FROM inbound WHERE update_id=1").fetchone()[0])
        assert transcript == {"status": "audio_unsupported", "text": "",
                              "hold_reason": "legacy_parameterized_audio_unresolved"}


def test_legacy_mp3_holds_bot_update_without_crashing_companion(tmp_path):
    import hashlib
    import json

    from bot.media import MediaStore

    _cfg(tmp_path)
    event = _update(1, photo=False)
    event["message"].pop("text")
    event["message"]["audio"] = {"file_id": "legacy-mp3", "file_size": 3,
                                 "mime_type": "audio/mpeg"}
    media = MediaStore(tmp_path)
    old_file = media.root / "1.ogg"
    old_file.write_bytes(b"mp3")
    (media.root / "1.json").write_text(json.dumps({
        "update_id": 1, "kind": "audio", "file_id": "legacy-mp3", "mime": "audio/mpeg",
        "status": "ready", "bytes": 3, "sha256": hashlib.sha256(b"mp3").hexdigest(),
    }))
    host = _Host()
    run(tmp_path, bot=_Bot([event]), host=host, max_polls=1)
    run(tmp_path, bot=_Bot([event]), host=host, max_polls=1)  # same offset, no process crash
    assert not host.turns and old_file.read_bytes() == b"mp3"
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}
        row = store.db.execute("SELECT transcript FROM inbound WHERE update_id=1").fetchone()
        assert json.loads(row[0])["status"] == "legacy_audio_suffix_ambiguous"


def test_legacy_ogg_label_with_mp3_bytes_is_held_without_host_turn(tmp_path):
    import hashlib
    import json

    from bot.media import MediaStore

    _cfg(tmp_path)
    event = _update(1, photo=False)
    event["message"].pop("text")
    event["message"]["voice"] = {"file_id": "old-voice", "file_size": 7}
    media = MediaStore(tmp_path)
    (media.root / "1.ogg").write_bytes(b"ID3fake")
    (media.root / "1.json").write_text(json.dumps({
        "update_id": 1, "kind": "voice", "file_id": "old-voice", "mime": "audio/ogg",
        "status": "ready", "bytes": 7, "sha256": hashlib.sha256(b"ID3fake").hexdigest(),
    }))
    host = _Host()
    run(tmp_path, bot=_Bot([event]), host=host, max_polls=1)
    assert not host.turns and (media.root / "1.ogg").read_bytes() == b"ID3fake"
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}
        row = store.db.execute("SELECT transcript FROM inbound WHERE update_id=1").fetchone()
        assert json.loads(row[0])["status"] == "legacy_audio_suffix_ambiguous"


def test_bot_legacy_valid_ogg_still_resolves(tmp_path):
    import hashlib
    import json

    from bot.media import MediaStore

    media = MediaStore(tmp_path)
    data = b"OggS-real-audio"
    (media.root / "1.ogg").write_bytes(data)
    (media.root / "1.json").write_text(json.dumps({
        "update_id": 1, "kind": "voice", "file_id": "old-voice", "mime": "audio/ogg",
        "status": "ready", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }))
    assert media.ready_file(1)[0].read_bytes() == data


def test_second_bot_manifest_declares_separate_credentials_and_widget_permission():
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text

    manifest = parse_skill_manifest_text((_SOURCE / "SKILL.md").read_text(encoding="utf-8"))
    assert manifest.name == "telegram-presence-bot"
    assert "TELEGRAM_PRESENCE_BOT_TOKEN" in manifest.env_from_settings
    assert "TELEGRAM_BOT_TOKEN" not in manifest.env_from_settings
    assert {"presence", "widget", "route", "transcribe_audio"}.issubset(manifest.permissions)
    assert not manifest.validate()


def test_second_bot_settings_schema_runs_through_host_validator():
    import importlib.util

    from ouroboros.extension_ui_validation import validate_settings_schema

    name = "test_telegram_presence_payload"
    spec = importlib.util.spec_from_file_location(
        name, _SOURCE / "plugin.py", submodule_search_locations=[str(_SOURCE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        result = validate_settings_schema(module._SETTINGS_SCHEMA)
        assert result["components"]
    finally:
        sys.modules.pop(name, None)
        for key in list(sys.modules):
            if key.startswith(name + "."):
                sys.modules.pop(key, None)


def test_unbound_text_cannot_exhaust_media_inbox_before_bound_photo(tmp_path):
    _cfg(tmp_path)

    class Batches(_Bot):
        def __init__(self):
            super().__init__([])
            self.batches = [
                [_update(uid, chat=-1002, photo=False) for uid in range(i, i + 100)]
                for i in range(1, 1001, 100)
            ] + [[_update(1001)]]

        def api(self, method, payload, **kwargs):
            if method == "getUpdates":
                self.calls.append((method, payload))
                return self.batches.pop(0)
            return super().api(method, payload, **kwargs)

    host = _Host()
    run(tmp_path, bot=Batches(), host=host, max_polls=11)
    assert len(host.turns) == 1
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.offset() == 1002
        assert store.counts() == {"done": 1}


def test_transient_media_read_retries_same_frozen_update_before_host_turn(tmp_path):
    from bot.transport import TransportUnavailable

    _cfg(tmp_path)

    class FlakyBot(_Bot):
        def __init__(self):
            super().__init__([_update(1)])
            self.downloads = 0

        def download(self, file_id, target, *, byte_limit):
            self.downloads += 1
            if self.downloads == 1:
                raise TransportUnavailable("first read timed out")
            return super().download(file_id, target, byte_limit=byte_limit)

    bot, host = FlakyBot(), _Host()
    run(tmp_path, bot=bot, host=host, max_polls=2)
    assert bot.downloads == 2 and len(host.turns) == 1
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"done": 1}
        assert store.offset() == 2


def test_video_and_document_download_are_distinct_opt_in_choices(tmp_path):
    import hashlib

    from bot.media import MediaStore, describe

    class Files:
        def __init__(self):
            self.calls = []

        def download(self, file_id, target, *, byte_limit):
            self.calls.append(file_id)
            Path(target).write_bytes(b"bytes")
            return 5, hashlib.sha256(b"bytes").hexdigest()

    media, transport = MediaStore(tmp_path), Files()
    config = empty()
    for index, kind in enumerate(("video", "document"), start=1):
        message = {kind: {"file_id": kind, "file_size": 5}}
        descriptor = describe(message)
        previous_calls = len(transport.calls)
        assert media.capture(index, descriptor, config, transport)["status"] == "download_disabled"
        assert len(transport.calls) == previous_calls
        config["download_video" if kind == "video" else "download_documents"] = True
        result = media.capture(index + 10, descriptor, config, transport)
        assert result["status"] == "ready" and transport.calls[-1] == kind
        assert media.ready_file(index + 10)[0].read_bytes() == b"bytes"


def test_missing_declared_size_over_policy_limit_is_terminal_media_receipt(tmp_path):
    from bot.media import MediaStore
    from bot.transport import MediaTooLarge

    class TooBig:
        def download(self, _file_id, _target, *, byte_limit):
            raise MediaTooLarge("provider getFile reports oversized")

    media = MediaStore(tmp_path)
    descriptor = {"kind": "photo", "file_id": "image-id", "size": None, "mime": "image/jpeg"}
    assert media.capture(1, descriptor, empty(), TooBig())["status"] == "media_too_large"
    assert media.ready_file(1) is None


def test_lost_host_reply_then_binding_rotation_does_not_readmit_old_source(tmp_path):
    from bot.transport import TransportUnavailable

    _cfg(tmp_path)
    update = _update(1, photo=False)

    class AmbiguousHost(_Host):
        def turn(self, binding, event, files):
            self.turns.append((binding, event, files))
            raise TransportUnavailable("Host accepted, response lost")

    host = AmbiguousHost()
    run(tmp_path, bot=_Bot([update]), host=host, max_polls=1)
    with Store(tmp_path / "transport.sqlite3") as store:
        row = store.pending()[0]
        assert row["binding_id"] == "a" * 32 and not row["work_ref"]
    config = empty()
    config["bindings"] = [{"chat_id": -1001, "topic_id": 42, "binding_id": "b" * 32}]
    save(tmp_path, config)
    run(tmp_path, bot=_Bot([update]), host=host, max_polls=1)
    assert len(host.turns) == 1
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}


def test_orphan_bot_media_temp_counts_against_private_budget(tmp_path, monkeypatch):
    from bot import media as module

    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 8)
    store = module.MediaStore(tmp_path)
    (store.root / ".interrupted.tmp").write_bytes(b"orphan!")
    assert store._used() == 7

    class Downloader:
        def download(self, *_args, **_kwargs):
            raise AssertionError("no network request after budget refusal")

    descriptor = module.describe({"photo": [{"file_id": "photo", "file_size": 3}]})
    with pytest.raises(ValueError, match="byte budget"):
        store.capture(1, descriptor, {"download_video": False, "download_documents": False}, Downloader())


@pytest.mark.parametrize("outcome", ["message", "deferred"])
def test_overlong_host_reply_is_held_without_stopping_other_intake(tmp_path, outcome):
    import json

    _cfg(tmp_path)
    bot = _Bot([_update(1)])

    class LongReply(_Host):
        def turn(self, binding, event, files):
            self.turns.append((binding, event, files))
            return {"ok": True, "status": "completed", "outcome": outcome,
                    "work_ref": "deferred-work" if outcome == "deferred" else "",
                    "text": "x" * 4097}

    host = LongReply()
    run(tmp_path, bot=bot, host=host, max_polls=1)
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}
        row = store.db.execute("SELECT body, transcript FROM inbound WHERE update_id=1").fetchone()
        reason = json.loads(row["transcript"])
        assert row["body"] and reason["hold_reason"] == "reply_too_long"
        assert reason["reply_bytes"] == 4097
    assert len(host.turns) == 1 and not any(method == "sendMessage" for method, _ in bot.calls)
    run(tmp_path, bot=bot, host=host, max_polls=1)
    assert len(host.turns) == 1  # restart cannot replay this overlong reply


@pytest.mark.parametrize("bound_at_capture", [True, False])
def test_mapping_is_frozen_with_update_before_presence_processing(tmp_path, bound_at_capture):
    from bot.config import empty, save

    a, b = "a" * 32, "b" * 32
    if bound_at_capture:
        _cfg(tmp_path)  # exact -1001 / topic 42 mapped to A
    else:
        save(tmp_path, empty())  # capture media privately without any Presence right
    update = _update(1)
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.capture(update, binding_id=a if bound_at_capture else None) == "pending"
        row = store.db.execute("SELECT binding_id FROM inbound WHERE update_id=1").fetchone()
        assert row["binding_id"] == (a if bound_at_capture else "unbound")
    changed = empty()
    changed["bindings"] = [{"chat_id": -1001, "topic_id": 42, "binding_id": b}]
    save(tmp_path, changed)
    bot, host = _Bot([update]), _Host()
    run(tmp_path, bot=bot, host=host, max_polls=1)
    assert not host.turns and not any(method == "sendMessage" for method, _ in bot.calls)
    with Store(tmp_path / "transport.sqlite3") as store:
        row = store.db.execute("SELECT status,binding_id,body FROM inbound WHERE update_id=1").fetchone()
        assert row["status"] == "held" and row["body"]
        assert row["binding_id"] == (a if bound_at_capture else "unbound")
    assert (tmp_path / "media" / "1.jpg").read_bytes() == PHOTO  # private bytes retained


def test_bot_binding_revoked_during_transcription_holds_source(tmp_path):
    import hashlib
    from bot.config import empty
    from bot.media import MediaStore, describe

    _cfg(tmp_path)
    update = _update(1, photo=False)
    update["message"].pop("text")
    update["message"]["voice"] = {"file_id": "voice-1", "file_size": 14}
    audio = b"OggSvoice-data"

    class Downloader:
        def download(self, _file_id, target, *, byte_limit):
            assert byte_limit >= len(audio)
            Path(target).write_bytes(audio)
            return len(audio), hashlib.sha256(audio).hexdigest()

    media = MediaStore(tmp_path)
    media.capture(1, describe(update["message"]), empty(), Downloader())

    class RevokingHost(_Host):
        def transcribe(self, *_args):
            save(tmp_path, empty())  # owner removes A while transcription awaits
            return "heard", "ready"

        def turn(self, *_args):
            raise AssertionError("revoked binding reached Host Presence")

    bot = _Bot([update])
    run(tmp_path, bot=bot, host=RevokingHost(), max_polls=1)
    with Store(tmp_path / "transport.sqlite3") as store:
        assert store.counts() == {"held": 1}
        assert store.db.execute("SELECT binding_id FROM inbound").fetchone()[0] == "a" * 32
    assert media.ready_file(1)[0].read_bytes() == audio
    assert not any(method == "sendMessage" for method, _ in bot.calls)


def test_bot_rechecks_binding_after_token_separation_before_send(tmp_path):
    from bot.config import load, empty
    from bot.runtime import send_one
    from bot.transport import TransportUnavailable

    _cfg(tmp_path)

    class RotatingBot(_Bot):
        def api(self, method, payload, **kwargs):
            assert method == "sendMessage"
            save(tmp_path, empty())  # emulate a blocking Host separation check
            if not kwargs["pre_send"]():
                raise TransportUnavailable("revoked before physical send")
            raise AssertionError("revoked mapping reached Telegram")

    with Store(tmp_path / "transport.sqlite3") as store:
        store.queue_reply("source", -1001, 42, 12, "a" * 32, "old reply")
        bot = RotatingBot([])
        assert send_one(store, load(tmp_path), bot, _Host(), 777, tmp_path) is True
        assert store.db.execute("SELECT status FROM outbound").fetchone()[0] == "unknown"
        assert not any(method == "sendMessage" for method, _ in bot.calls)


def test_real_bot_transport_checks_revocation_after_host_separation(tmp_path):
    from bot.config import binding_for, load, empty
    from bot.transport import BotTransport, TransportUnavailable

    _cfg(tmp_path)
    requests = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _max_bytes):
            return b'{"ok":true,"status":"distinct"}'

    class Opener:
        def open(self, req, *, timeout):
            requests.append(req.full_url)
            assert req.full_url == "http://127.0.0.1:8767/telegram/presence-bot-token"
            save(tmp_path, empty())
            return Reply()

    bot = BotTransport("second-token", "http://127.0.0.1:8767", "host-private", opener=Opener())
    with pytest.raises(TransportUnavailable, match="revoked"):
        bot.api("sendMessage", {"chat_id": -1001, "text": "old reply"},
                pre_send=lambda: binding_for(load(tmp_path), -1001, 42) == "a" * 32)
    assert requests == ["http://127.0.0.1:8767/telegram/presence-bot-token"]


def test_silent_audio_is_not_misreported_as_missing_transcriber():
    from bot.runtime import _media_text

    event = {"message_id": 12}
    receipt = {"kind": "voice", "status": "ready"}
    text = _media_text(event, receipt, {"status": "no_speech", "text": ""})
    assert "No speech detected" in text and "transcription unavailable" not in text


@pytest.mark.parametrize("change", ["rotate", "remove", "unrelated"])
def test_longpoll_config_change_cannot_authorize_old_topic_speech(tmp_path, change):
    from bot.config import empty, save

    _cfg(tmp_path)
    update = _update(1)

    class EditingBot(_Bot):
        def api(self, method, payload, **kwargs):
            if method == "getUpdates":
                config = empty()
                if change != "remove":
                    config["bindings"] = [{"chat_id": -1001, "topic_id": 42,
                                           "binding_id": ("b" if change == "rotate" else "a") * 32}]
                if change == "unrelated":
                    config["bindings"].append({"chat_id": -1002, "topic_id": 42,
                                               "binding_id": "c" * 32})
                save(tmp_path, config)  # owner writes during a 25-second longpoll
            return super().api(method, payload, **kwargs)

    bot, host = EditingBot([update]), _Host()
    run(tmp_path, bot=bot, host=host, max_polls=1)
    with Store(tmp_path / "transport.sqlite3") as store:
        row = store.db.execute("SELECT binding_id,status FROM inbound WHERE update_id=1").fetchone()
        if change == "unrelated":
            assert row["binding_id"] == "a" * 32 and row["status"] == "done"
            assert len(host.turns) == 1 and host.turns[0][0] == "a" * 32
        else:
            assert row["binding_id"] == "unbound" and row["status"] == "held"
            assert not host.turns and not any(method == "sendMessage" for method, _ in bot.calls)
    assert (tmp_path / "media" / "1.jpg").read_bytes() == PHOTO
