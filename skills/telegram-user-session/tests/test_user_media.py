"""Offline source-only checks of the optional user-account media inbox."""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SOURCE = Path(__file__).resolve().parents[1]
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from userbot.config import Config, save_config  # noqa: E402
from userbot.media_inbox import MediaInbox, MediaLimit, describe  # noqa: E402
from userbot.presence import PresenceResult  # noqa: E402
from userbot.runtime import UserSessionBridge  # noqa: E402
from userbot.store import DurableStore  # noqa: E402


def _message(*, kind="voice", data=b"voice", mid=12, mime=None):
    return SimpleNamespace(
        id=mid, media=object(), photo=object() if kind == "photo" else None,
        voice=kind == "voice", audio=kind == "audio", video=kind == "video",
        document=object() if kind == "document" else None,
        file=SimpleNamespace(size=len(data), mime_type=mime or ("image/jpeg" if kind == "photo" else "audio/ogg")),
    )


class _Client:
    def __init__(self, message, data):
        self.message, self.data, self.fetches = message, data, 0

    async def get_messages(self, peer, ids):
        assert peer == -1001 and ids == self.message.id
        return self.message

    async def iter_download(self, media, request_size):
        self.fetches += 1
        assert media is self.message.media and request_size == 65536
        for offset in range(0, len(self.data), 4):
            yield self.data[offset:offset + 4]


def _filename():
    return hashlib.sha256(b"one-provider-event").hexdigest() + ".json"


def test_unbound_photo_bytes_captured_before_any_presence_binding(tmp_path):
    message = _message(kind="photo", data=b"photo-pixels")
    client = _Client(message, b"photo-pixels")
    descriptor = describe(message)
    inbox = MediaInbox(tmp_path)
    config = SimpleNamespace(download_video=False, download_documents=False)
    result = asyncio.run(inbox.ensure(_filename(), descriptor, config,
                                      SimpleNamespace(client=client), -1001, 12))
    assert result["status"] == "ready" and client.fetches == 1
    file, receipt = inbox.ready_file(_filename())
    assert file.read_bytes() == b"photo-pixels" and receipt == result
    # A replay reads immutable captured bytes, never Telegram again.
    assert asyncio.run(inbox.ensure(_filename(), descriptor, config,
                                    SimpleNamespace(client=client), -1001, 12)) == result
    assert client.fetches == 1
    file.write_bytes(b"other-pixels")
    with pytest.raises(MediaLimit, match="changed"):
        inbox.ready_file(_filename())


@pytest.mark.parametrize("kind,choice", [("video", "download_video"), ("document", "download_documents")])
def test_optional_video_and_files_do_not_download_by_default(tmp_path, kind, choice):
    message = _message(kind=kind)
    client = _Client(message, b"voice")
    config = SimpleNamespace(download_video=False, download_documents=False)
    inbox = MediaInbox(tmp_path)
    result = asyncio.run(inbox.ensure(_filename(), describe(message), config,
                                      SimpleNamespace(client=client), -1001, 12))
    assert result["status"] == "download_disabled" and client.fetches == 0
    # Policy edits cannot quietly rewrite an older source's refusal.
    setattr(config, choice, True)
    assert asyncio.run(inbox.ensure(_filename(), describe(message), config,
                                    SimpleNamespace(client=client), -1001, 12)) == result
    assert client.fetches == 0


def test_raw_capture_orders_unbound_media_and_rejects_changed_replay(tmp_path):
    account, filename = "123", _filename()
    base = {"account_id": 123, "peer_id": -1001, "message_id": 12, "kind": "new",
            "revision": "", "actor_id": None, "text": "", "mentioned": False,
            "forum_topic": True, "reply_to_top_id": 42, "reply_to_msg_id": 42,
            "action_constructor_id": None, "media": {"kind": "photo", "size": 10, "mime": "image/jpeg"}}
    with DurableStore(tmp_path / "inbox.sqlite3") as store:
        assert store.stage_raw(account, filename, base) == "created"
        assert store.stage_raw(account, filename, base) == "pending"
        seq, name, raw = store.next_raw(account, set())
        assert name == filename and raw["peer_id"] == -1001 and raw["reply_to_top_id"] == 42
        with pytest.raises(ValueError, match="identity changed"):
            store.stage_raw(account, filename, dict(base, media={"kind": "photo", "size": 11,
                                                       "mime": "image/jpeg"}))
        store.settle_raw(account, filename, held=True)
        assert store.next_raw(account, set()) is None


def _bridge(tmp_path, *, bound=False):
    class Adapter:
        def __init__(self):
            self.client = None

        async def topic_ids(self, peer):
            return None  # ordinary non-forum peer

    class Presence:
        binding_id = "a" * 32

        def __init__(self):
            self.calls = []

        def turn(self, event, **kwargs):
            self.calls.append((event, kwargs))
            return PresenceResult("completed", outcome="silent")

        def transcribe_media(self, **kwargs):
            assert kwargs["peer_id"] == -1001 and kwargs["audio"] == b"voice"
            return "recognized locally", "ready"

    config = Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                    allowed_peer_ids=(-1001,) if bound else (),
                    binding_id="a" * 32 if bound else None, account_id=123)
    save_config(tmp_path, config)
    adapter = Adapter()
    presence = Presence()
    store = DurableStore(tmp_path / "transport.sqlite3")
    bridge = UserSessionBridge(tmp_path, config, adapter, store, presence)
    return bridge, store, adapter, presence


def _event(message, *, sender=9):
    message.out = False
    message.reply_to = None
    message.mentioned = False
    return SimpleNamespace(message=message, chat_id=-1001, sender_id=sender, raw_text="")


def test_unallowlisted_media_only_is_private_bytes_not_a_presence_turn(tmp_path):
    bridge, store, adapter, presence = _bridge(tmp_path)
    message = _message(kind="photo", data=b"image")
    adapter.client = _Client(message, b"image")
    path = bridge.spool_update(_event(message))
    assert path and store.pending_raw_count("123") == 1
    try:
        assert asyncio.run(bridge.process_incoming()) == 0
        assert store.pending_raw_count("123") == 0
        assert bridge.media.ready_file(path.name)[0].read_bytes() == b"image"
        assert path.with_suffix(".held").exists() and not presence.calls
    finally:
        store.close()


def test_bound_photo_goes_to_host_as_stable_staged_file(tmp_path):
    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="photo", data=b"image")
    adapter.client = _Client(message, b"image")
    path = bridge.spool_update(_event(message))
    try:
        assert asyncio.run(bridge.process_incoming()) == 1
        assert len(presence.calls) == 1
        event, options = presence.calls[0]
        assert event["conversation_id"] == "-1001" and event["thread_id"] == ""
        assert event["message"]["media"]["status"] == "ready"
        assert Path(options["staged_files"][0]).read_bytes() == b"image"
        assert not path.exists() and bridge.media.ready_file(path.name) is not None
    finally:
        store.close()


def test_bound_voice_transcript_and_source_are_frozen_before_host_turn(tmp_path):
    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="voice")
    adapter.client = _Client(message, b"voice")
    path = bridge.spool_update(_event(message))
    try:
        assert asyncio.run(bridge.process_incoming()) == 1
        event, options = presence.calls[0]
        assert "recognized locally" in event["text"]
        assert Path(options["staged_files"][0]).read_bytes() == b"voice"
        assert bridge.media.ready_file(path.name)[1]["status"] == "ready"
    finally:
        store.close()


@pytest.mark.parametrize("mime,suffix,host_mime", [
    ("audio/mpeg", ".mp3", "audio/mpeg"),
    ("audio/ogg", ".ogg", "audio/ogg"),
    ("audio/x-unknown", ".bin", "application/octet-stream"),
])
def test_audio_capture_suffix_matches_host_attachment(tmp_path, mime, suffix, host_mime):
    from ouroboros.artifacts import stage_task_attachments

    data = (b"OggSfake-audio" if mime == "audio/ogg" else
            b"ID3fake-audio" if mime == "audio/mpeg" else b"opaque-audio")
    message = _message(kind="audio", data=data, mime=mime)
    client = _Client(message, data)
    inbox = MediaInbox(tmp_path)
    result = asyncio.run(inbox.ensure(
        _filename(), describe(message), SimpleNamespace(download_video=False, download_documents=False),
        SimpleNamespace(client=client), -1001, 12))
    assert result["status"] == "ready" and result["mime"] == mime
    source, _ = inbox.ready_file(_filename())
    assert source.suffix == suffix and source.read_bytes() == data
    attached = stage_task_attachments(tmp_path, "audio-task", [{"path": str(source)}])
    assert attached[0]["mime"] == host_mime and attached[0]["is_image"] is False


def test_parameterized_audio_mime_is_normalized_before_transcription():
    assert describe(_message(kind="audio", mime="audio/ogg; codecs=opus"))["mime"] == "audio/ogg"
    assert describe(_message(kind="audio", mime="audio/webm;codecs=opus"))["mime"] == "audio/webm"
    message = _message(kind="audio")
    message.file.mime_type = None
    assert describe(message)["mime"] == "application/octet-stream"


def test_old_parameterized_ogg_user_receipt_transcribes_as_ogg(tmp_path, monkeypatch):
    import json

    from userbot import media_transcribe

    inbox = MediaInbox(tmp_path)
    file = inbox.root / (_filename()[:64] + ".ogg")
    file.write_bytes(b"OggS-old-audio")
    receipt = {"source": _filename(), "kind": "audio", "mime": "audio/ogg;codecs=opus",
               "suffix": ".ogg", "status": "ready", "bytes": 14,
               "sha256": hashlib.sha256(b"OggS-old-audio").hexdigest()}
    (inbox.root / (_filename()[:64] + ".json")).write_text(json.dumps(receipt))
    source, saved = inbox.ready_file(_filename())
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

    monkeypatch.setattr(media_transcribe.request, "build_opener", lambda *_: Opener())
    token = SimpleNamespace(use_in_request=lambda: "private")
    assert media_transcribe.transcribe("http://127.0.0.1:8767", token, binding_id="a" * 32,
                                      account_id=123, peer_id=-1001, topic_id=None,
                                      audio=source.read_bytes(), mime=saved["mime"]) == ("heard", "ready")
    assert sent == [("audio/ogg", b"OggS-old-audio", 120)]
    assert saved["mime"] == "audio/ogg;codecs=opus"


def test_captured_mp3_reaches_audio_transcriber_without_falsifying_mime(tmp_path, monkeypatch):
    from userbot import media_transcribe

    payload = b"ID3short-audio"
    message = _message(kind="audio", data=payload, mime="audio/mpeg")
    inbox = MediaInbox(tmp_path)
    asyncio.run(inbox.ensure(_filename(), describe(message),
                             SimpleNamespace(download_video=False, download_documents=False),
                             SimpleNamespace(client=_Client(message, payload)), -1001, 12))
    source, receipt = inbox.ready_file(_filename())
    sent = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b'{"text":"heard mp3"}'

    class Opener:
        def open(self, req, *, timeout):
            sent.append((req.get_header("Content-type"), req.data, timeout))
            return Reply()

    monkeypatch.setattr(media_transcribe.request, "build_opener", lambda *_: Opener())
    token = SimpleNamespace(use_in_request=lambda: "private")
    assert media_transcribe.transcribe("http://127.0.0.1:8767", token, binding_id="a" * 32,
                                      account_id=123, peer_id=-1001, topic_id=None,
                                      audio=source.read_bytes(), mime=receipt["mime"]) == ("heard mp3", "ready")
    assert source.suffix == ".mp3" and sent == [("audio/mpeg", payload, 120)]


def test_old_unsupported_transcription_holds_ambiguous_user_turn(tmp_path):
    import json

    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="audio", data=b"OggS-old-audio", mime="audio/ogg;codecs=opus")
    adapter.client = _Client(message, b"OggS-old-audio")
    path = bridge.spool_update(_event(message))
    (bridge.media.root / (path.name[:64] + ".ogg")).write_bytes(b"OggS-old-audio")
    receipt = {"source": path.name, "kind": "audio", "mime": "audio/ogg;codecs=opus",
               "suffix": ".ogg", "status": "ready", "bytes": 14,
               "sha256": hashlib.sha256(b"OggS-old-audio").hexdigest()}
    (bridge.media.root / (path.name[:64] + ".json")).write_text(json.dumps(receipt))
    raw = json.loads(path.read_text())
    raw["transcription"] = {"status": "audio_unsupported", "text": ""}
    path.write_text(json.dumps(raw))  # pre-fix decision saved before uncertain Host POST
    try:
        assert asyncio.run(bridge.process_incoming()) == 0
        held = path.with_suffix(".held")
        assert held.exists()
        assert json.loads(held.read_text())["failure_code"] == "LEGACY_AUDIO_TRANSCRIPTION_UNRESOLVED"
        assert not presence.calls and store.pending_raw_count("123") == 0
        assert (bridge.media.root / (path.name[:64] + ".ogg")).read_bytes() == b"OggS-old-audio"
        assert asyncio.run(bridge.process_incoming()) == 0
    finally:
        store.close()


def test_audio_suffix_budget_and_legacy_receipt_are_fail_closed(tmp_path):
    import json

    inbox = MediaInbox(tmp_path)
    message = _message(kind="audio", data=b"mp3", mime="audio/mpeg")
    descriptor = describe(message)
    client = _Client(message, b"mp3")
    config = SimpleNamespace(download_video=False, download_documents=False)
    first = asyncio.run(inbox.ensure(_filename(), descriptor, config,
                                     SimpleNamespace(client=client), -1001, 12))
    assert first["suffix"] == ".mp3" and inbox._used() == 3
    receipt_file = inbox.root / (_filename()[:64] + ".json")
    (inbox.root / (_filename()[:64] + ".mp3")).rename(
        inbox.root / (_filename()[:64] + ".ogg"))  # old version's physical naming
    receipt_file.write_text(json.dumps({k: v for k, v in first.items() if k != "suffix"}))
    with pytest.raises(MediaLimit, match="legacy audio suffix ambiguous"):
        inbox.ready_file(_filename())
    assert (inbox.root / (_filename()[:64] + ".ogg")).read_bytes() == b"mp3"


def test_legacy_mp3_event_is_held_with_bytes_and_no_presence_turn(tmp_path):
    import json

    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="audio", data=b"mp3", mime="audio/mpeg")
    adapter.client = _Client(message, b"mp3")
    path = bridge.spool_update(_event(message))
    old_file = bridge.media.root / (path.name[:64] + ".ogg")
    old_file.write_bytes(b"mp3")
    old_receipt = {"source": path.name, "kind": "audio", "mime": "audio/mpeg",
                   "status": "ready", "bytes": 3, "sha256": hashlib.sha256(b"mp3").hexdigest()}
    (bridge.media.root / (path.name[:64] + ".json")).write_text(json.dumps(old_receipt))
    try:
        assert asyncio.run(bridge.process_incoming()) == 0
        held = path.with_suffix(".held")
        assert held.exists() and json.loads(held.read_text())["failure_code"] == "LEGACY_AUDIO_SUFFIX_AMBIGUOUS"
        assert old_file.read_bytes() == b"mp3" and not presence.calls
        assert store.pending_raw_count("123") == 0
        assert asyncio.run(bridge.process_incoming()) == 0  # restarted worker must not retry it
    finally:
        store.close()


def test_legacy_generic_audio_falsely_labelled_ogg_is_held(tmp_path):
    import json

    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="audio", data=b"ID3fake", mime="audio/ogg")
    message.file.mime_type = None  # old generic-audio fallback inferred Ogg
    adapter.client = _Client(message, b"ID3fake")
    path = bridge.spool_update(_event(message))
    old_file = bridge.media.root / (path.name[:64] + ".ogg")
    old_file.write_bytes(b"ID3fake")
    (bridge.media.root / (path.name[:64] + ".json")).write_text(json.dumps({
        "source": path.name, "kind": "audio", "mime": "audio/ogg", "status": "ready",
        "bytes": 7, "sha256": hashlib.sha256(b"ID3fake").hexdigest(),
    }))
    try:
        assert asyncio.run(bridge.process_incoming()) == 0
        assert json.loads(path.with_suffix(".held").read_text())["failure_code"] == "LEGACY_AUDIO_SUFFIX_AMBIGUOUS"
        assert old_file.read_bytes() == b"ID3fake" and not presence.calls
        assert store.pending_raw_count("123") == 0
    finally:
        store.close()


def test_legacy_verified_ogg_still_resolves(tmp_path):
    import json

    inbox = MediaInbox(tmp_path)
    name = _filename()
    data = b"OggS-real-audio"
    (inbox.root / (name[:64] + ".ogg")).write_bytes(data)
    (inbox.root / (name[:64] + ".json")).write_text(json.dumps({
        "source": name, "kind": "audio", "mime": "audio/ogg", "status": "ready",
        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }))
    assert inbox.ready_file(name)[0].read_bytes() == data


def test_unsupported_user_media_keeps_private_typed_receipt(tmp_path):
    message = _message(kind="document")
    message.sticker = True
    descriptor = describe(message)
    assert descriptor["kind"] == "unsupported"
    client = _Client(message, b"voice")
    result = asyncio.run(MediaInbox(tmp_path).ensure(
        _filename(), descriptor, SimpleNamespace(download_video=False, download_documents=False),
        SimpleNamespace(client=client), -1001, 12))
    assert result["status"] == "unsupported_media" and client.fetches == 0


def test_unallowlisted_plain_text_cannot_starve_next_photo(tmp_path):
    bridge, store, adapter, presence = _bridge(tmp_path)
    text = _message(kind="voice")
    text.media = None
    for uid in range(1, 1002):
        text.id = uid
        event = _event(text)
        event.raw_text = "unrelated text"
        assert bridge.spool_update(event) is None
    assert store.pending_raw_count("123") == 0
    image = _message(kind="photo", data=b"image", mid=1002)
    adapter.client = _Client(image, b"image")
    event = _event(image)
    path = bridge.spool_update(event)
    try:
        assert path and asyncio.run(bridge.process_incoming()) == 0
        assert bridge.media.ready_file(path.name)[0].read_bytes() == b"image"
        assert not presence.calls
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["lookup", "stream"])
def test_transient_media_provider_failure_keeps_raw_event_for_replay(tmp_path, stage):
    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="photo", data=b"image")

    class Flaky(_Client):
        async def get_messages(self, peer, ids):
            if stage == "lookup":
                raise OSError("temporary Telegram lookup failure")
            return await super().get_messages(peer, ids)

        async def iter_download(self, media, request_size):
            if stage == "stream":
                yield b"im"
                raise OSError("temporary Telegram stream failure")
            async for chunk in super().iter_download(media, request_size):
                yield chunk

    adapter.client = Flaky(message, b"image")
    path = bridge.spool_update(_event(message))
    bridge._observed_media.clear()  # cold replay must fetch by frozen provider identity
    try:
        assert asyncio.run(bridge.process_incoming()) == 0
        assert path.exists() and store.pending_raw_count("123") == 1
        assert bridge._ingress_failures[-1001][0] == "MEDIA_FETCH_UNAVAILABLE"
        assert not presence.calls and bridge.media.ready_file(path.name) is None
        # The provider is healthy again. This source's original bytes/identity
        # are replayed without requiring companion restart or a new event.
        bridge._blocked_until.clear()
        adapter.client = _Client(message, b"image")
        assert asyncio.run(bridge.process_incoming()) == 1
        assert bridge.media.ready_file(path.name)[0].read_bytes() == b"image"
    finally:
        store.close()


def test_orphan_temp_bytes_count_against_private_inbox_budget(tmp_path, monkeypatch):
    from userbot import media_inbox

    monkeypatch.setattr(media_inbox, "INBOX_BYTES", 8)
    inbox = MediaInbox(tmp_path)
    (inbox.root / ".interrupted.tmp").write_bytes(b"orphan!")
    assert inbox._used() == 7
    message = _message(kind="photo", data=b"img")
    with pytest.raises(MediaLimit, match="byte budget"):
        asyncio.run(inbox.ensure(_filename(), describe(message),
                                 SimpleNamespace(download_video=False, download_documents=False),
                                 SimpleNamespace(client=_Client(message, b"img")), -1001, 12))


@pytest.mark.parametrize("revoke_a", [False, True])
def test_other_peer_edit_keeps_admitted_deferred_work_but_revocation_holds_it(tmp_path, revoke_a):
    from userbot.config import Binding

    bridge, store, adapter, _presence = _bridge(tmp_path, bound=True)
    a, b = "a" * 32, "b" * 32
    bridge.config = Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                           allowed_peer_ids=(-1001,), account_id=123,
                           bindings=(Binding(-1001, None, a),))
    save_config(tmp_path, bridge.config)

    class Deferred:
        binding_id = a

        def __init__(self):
            self.turns = self.works = 0

        def turn(self, _event, **_kwargs):
            self.turns += 1
            return PresenceResult("completed", outcome="deferred", work_ref="work-A")

        def work(self, _work_ref):
            self.works += 1
            return PresenceResult("completed", outcome="silent", work_ref="work-A")

    host = Deferred()
    bridge.presence = host
    message = _message(kind="photo", data=b"image")
    adapter.client = _Client(message, b"image")
    path = bridge.spool_update(_event(message))
    try:
        assert asyncio.run(bridge.process_incoming()) == 0
        assert path.exists() and host.turns == 1 and host.works == 0
        raw = __import__("json").loads(path.read_text(encoding="utf-8"))
        assert raw["selected_binding"] == a and raw["work_ref"] == "work-A"
        bridge.config = Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                               allowed_peer_ids=(-1002,) if revoke_a else (-1002, -1001),
                               account_id=123,
                               bindings=((Binding(-1002, None, b),) if revoke_a else
                                         (Binding(-1002, None, b), Binding(-1001, None, a))))
        save_config(tmp_path, bridge.config)
        result = asyncio.run(bridge.process_incoming())
        if revoke_a:
            assert result == 0 and host.works == 0
            assert path.with_suffix(".held").exists()
        else:
            assert result == 1 and host.works == 1 and not path.exists()
    finally:
        store.close()


def test_missing_private_source_after_durable_capture_never_rebinds_on_redelivery(tmp_path):
    import json

    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="photo", data=b"image")
    event = _event(message)
    original = bridge.spool_update(event)  # initial DB+file under binding A
    assert original is not None and original.exists()
    original.unlink()  # crash/loss across the DB-capture → private-file seam
    bridge.config = Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                           allowed_peer_ids=(-1001,), account_id=123, binding_id="b" * 32)
    save_config(tmp_path, bridge.config)
    try:
        assert bridge.spool_update(event) is None
        assert not original.exists() and store.pending_raw_count("123") == 0
        saved = store.db.execute("SELECT initial_json,status FROM raw_ingress WHERE filename=?",
                                 (original.name,)).fetchone()
        assert saved["status"] == "held"
        assert json.loads(saved["initial_json"])["binding_id"] == "a" * 32
        assert bridge._ingress_failures[-1001][0] == "RAW_SOURCE_MISSING"
        assert asyncio.run(bridge.process_incoming()) == 0 and not presence.calls
    finally:
        store.close()


def test_revocation_during_transcription_holds_original_without_host_turn(tmp_path):
    import json
    import threading

    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="voice", data=b"OggS-voice")
    adapter.client = _Client(message, b"OggS-voice")
    path = bridge.spool_update(_event(message))
    entered, release = threading.Event(), threading.Event()

    def slow_transcribe(**_kwargs):
        entered.set()
        assert release.wait(5)
        return "heard", "ready"

    presence.transcribe_media = slow_transcribe

    async def race():
        task = asyncio.create_task(bridge.process_incoming())
        assert await asyncio.to_thread(entered.wait, 5)
        # The companion's self.config still names A: the owner has only changed
        # the durable document while the transcription worker was suspended.
        save_config(tmp_path, Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                                    allowed_peer_ids=(-1001,), account_id=123,
                                    binding_id="b" * 32))
        release.set()
        return await task

    try:
        assert asyncio.run(race()) == 0
        held = path.with_suffix(".held")
        assert held.exists() and not path.exists() and not presence.calls
        assert json.loads(held.read_text())["selected_binding"] == "a" * 32
        assert bridge.media.ready_file(path.name)[0].read_bytes() == b"OggS-voice"
    finally:
        release.set()
        store.close()


def test_revocation_during_send_preflight_prevents_physical_send(tmp_path):
    bridge, store, adapter, _presence = _bridge(tmp_path, bound=True)
    bridge._queue_reply({"peer_id": -1001, "message_id": 12}, None, "a" * 32,
                        "reply", 1, "source-event", "turn")
    entered, release = asyncio.Event(), asyncio.Event()
    sends = []

    async def topic_ids(_peer):
        entered.set()
        await release.wait()
        return None

    async def send_text(*args, **kwargs):
        sends.append((args, kwargs))
        raise AssertionError("revoked send reached Telegram")

    async def get_messages(_peer, *, ids):
        return SimpleNamespace(id=ids)

    adapter.client = SimpleNamespace(get_messages=get_messages)
    adapter.topic_ids = topic_ids
    adapter.send_text = send_text

    async def race():
        task = asyncio.create_task(bridge.send_one())
        await asyncio.wait_for(entered.wait(), 5)
        save_config(tmp_path, Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                                    allowed_peer_ids=(-1001,), account_id=123,
                                    binding_id="b" * 32))
        release.set()
        return await task

    try:
        assert asyncio.run(race()) is True
        row = store.db.execute("SELECT status,error_code FROM outgoing").fetchone()
        assert row["status"] == "failed" and row["error_code"] == "BINDING_OR_PERMISSION_CHANGED"
        assert not sends
    finally:
        store.close()


def test_missing_spool_after_deferred_host_turn_holds_without_restarting_turn(tmp_path):
    import json

    bridge, store, adapter, _presence = _bridge(tmp_path, bound=True)

    class Deferred:
        binding_id = "a" * 32

        def __init__(self):
            self.turns = self.works = 0

        def turn(self, _event, **_options):
            self.turns += 1
            return PresenceResult("completed", outcome="deferred", work_ref="work-A")

        def work(self, _work_ref):
            self.works += 1
            raise AssertionError("missing source must not poll or replay Host")

    host = Deferred()
    bridge.presence = host
    message = _message(kind="photo", data=b"image")
    adapter.client = _Client(message, b"image")
    path = bridge.spool_update(_event(message))
    try:
        assert asyncio.run(bridge.process_incoming()) == 0
        assert json.loads(path.read_text())["work_ref"] == "work-A"
        assert host.turns == 1
        path.unlink()  # lost after Host acceptance, not the initial DB/file gap
        assert asyncio.run(bridge.process_incoming()) == 0
        assert host.turns == 1 and host.works == 0 and not path.exists()
        row = store.db.execute("SELECT status,initial_json FROM raw_ingress").fetchone()
        assert row["status"] == "held" and json.loads(row["initial_json"])["message_id"] == 12
        assert bridge._ingress_failures[-1001][0] == "RAW_SOURCE_MISSING"
        assert bridge.media.ready_file(path.name)[0].read_bytes() == b"image"
    finally:
        store.close()


def test_revocation_during_catalog_wait_prevents_host_transcription(tmp_path):
    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="voice", data=b"OggS-voice")
    adapter.client = _Client(message, b"OggS-voice")
    path = bridge.spool_update(_event(message))
    entered, release = asyncio.Event(), asyncio.Event()
    transcriptions = []

    async def topic_ids(_peer):
        entered.set()
        await release.wait()
        return None

    def transcribe(**kwargs):
        transcriptions.append(kwargs)
        raise AssertionError("revoked audio reached Host transcriber")

    adapter.topic_ids = topic_ids
    presence.transcribe_media = transcribe

    async def race():
        task = asyncio.create_task(bridge.process_incoming())
        await asyncio.wait_for(entered.wait(), 5)
        save_config(tmp_path, Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                                    allowed_peer_ids=(-1001,), account_id=123,
                                    binding_id="b" * 32))
        release.set()
        return await task

    try:
        assert asyncio.run(race()) == 0
        assert path.with_suffix(".held").exists() and not transcriptions and not presence.calls
        assert bridge.media.ready_file(path.name)[0].read_bytes() == b"OggS-voice"
    finally:
        store.close()


def test_real_mtproto_send_rechecks_after_entity_resolution(tmp_path):
    from userbot.telegram import TelegramAdapter

    bridge, store, _adapter, _presence = _bridge(tmp_path, bound=True)
    bridge._queue_reply({"peer_id": -1001, "message_id": 12}, None, "a" * 32,
                        "reply", 1, "source-event", "turn")
    entered, release = asyncio.Event(), asyncio.Event()
    physical = []

    class Client:
        async def get_messages(self, _peer, *, ids):
            return SimpleNamespace(id=ids)

        async def get_input_entity(self, _peer):
            entered.set()
            await release.wait()
            return object()

        async def is_user_authorized(self):
            return True

        async def __call__(self, request):
            physical.append(request)
            raise AssertionError("revoked message reached MTProto")

    adapter = TelegramAdapter(tmp_path / "mtproto.session", 123, "a" * 32, client=Client())

    async def topic_ids(_peer):
        return None

    adapter.topic_ids = topic_ids
    bridge.adapter = adapter

    async def race():
        task = asyncio.create_task(bridge.send_one())
        await asyncio.wait_for(entered.wait(), 5)
        save_config(tmp_path, Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                                    allowed_peer_ids=(-1001,), account_id=123,
                                    binding_id="b" * 32))
        release.set()
        return await task

    try:
        assert asyncio.run(race()) is True
        assert not physical
        # The intent crossed its durable attempted boundary, so recovery
        # conservatively reports UNKNOWN even though this test saw no send.
        assert store.db.execute("SELECT status FROM outgoing").fetchone()[0] == "attempted"
    finally:
        store.close()
    with DurableStore(tmp_path / "transport.sqlite3", recover_attempted=True) as recovered:
        assert recovered.db.execute("SELECT status FROM outgoing").fetchone()[0] == "unknown"


def test_action_rechecks_owner_after_second_target_await(tmp_path, monkeypatch):
    from telethon import types
    from userbot import actions as action_module
    from userbot.action_store import ActionStore

    bridge, store, adapter, _presence = _bridge(tmp_path, bound=True)
    action_store = ActionStore(tmp_path / "actions.sqlite3")
    action_store.enqueue("123", "delete-one", operation="delete", peer_id=-1001,
                         binding_id="a" * 32, topic_id=None, message_id=12)
    bridge.action_store = action_store
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    actions = action_module.Actions.__new__(action_module.Actions)
    actions.adapter = SimpleNamespace(client=object())

    async def target(_peer, _message, _topic):
        calls.append("target")
        if len(calls) == 2:  # Actions repeats target lookup after durable attempted mark.
            entered.set()
            await release.wait()
        return types.InputPeerChannel(channel_id=1, access_hash=2), SimpleNamespace()

    async def mutate(*_args):
        calls.append("physical mutation")
        raise AssertionError("revoked action reached Telegram")

    actions._own_target = target
    bridge._actions = actions
    monkeypatch.setattr(action_module, "_mutate", mutate)

    async def me():
        return 123

    adapter.get_me = me

    async def race():
        task = asyncio.create_task(bridge.process_action_once())
        await asyncio.wait_for(entered.wait(), 5)
        save_config(tmp_path, Config(api_id=123, api_hash="a" * 32, phone="+123456789",
                                    allowed_peer_ids=(-1001,), account_id=123,
                                    binding_id="b" * 32))
        release.set()
        return await task

    try:
        assert asyncio.run(race()) is True
        action = action_store.get("123", "delete-one")
        assert action.status == "unknown" and calls == ["target", "target"]
    finally:
        action_store.close()
        store.close()


def test_bound_silent_audio_keeps_source_and_names_silence_not_unavailability(tmp_path):
    bridge, store, adapter, presence = _bridge(tmp_path, bound=True)
    message = _message(kind="voice", data=b"OggS-silence")
    adapter.client = _Client(message, b"OggS-silence")
    presence.transcribe_media = lambda **_kwargs: ("", "no_speech")
    path = bridge.spool_update(_event(message))
    try:
        assert asyncio.run(bridge.process_incoming()) == 1
        assert len(presence.calls) == 1
        event, options = presence.calls[0]
        assert "No speech detected" in event["text"]
        assert "transcription unavailable" not in event["text"]
        assert Path(options["staged_files"][0]).read_bytes() == b"OggS-silence"
        assert bridge.media.ready_file(path.name)[0].read_bytes() == b"OggS-silence"
    finally:
        store.close()
