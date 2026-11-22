"""Offline regressions: no model download, CTranslate load or live installation."""
from __future__ import annotations

import asyncio
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from whisper_runtime import (  # noqa: E402
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_SECONDS,
    DICTATION_MAX_BYTES,
    DICTATION_MAX_SECONDS,
    DURATION_GRACE_SECONDS,
    ModelUnavailable,
    audio_format,
    model_directory,
    read_settings,
    validate_choice,
    write_settings,
)


def load_plugin():
    # Installed slug has a hyphen; simulate the extension loader's package.
    import importlib
    package = types.ModuleType("_voice_skill_test")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    return importlib.import_module("_voice_skill_test.plugin")


def make_model(tmp_path):
    model = tmp_path / "local-model"
    model.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model / name).write_bytes(b"valid")
    return model


def test_receipt_reads_existing_snapshot_and_never_downloads(tmp_path):
    model = make_model(tmp_path)
    receipt = tmp_path / "installed.json"
    receipt.write_text(json.dumps({"schema": "helene.voice.v1", "model": "turbo", "path": str(model)}), encoding="utf-8")
    assert model_directory(str(receipt)) == model
    assert read_settings(tmp_path)["always_resident"] is False
    write_settings(tmp_path, validate_choice(str(receipt), True))
    assert read_settings(tmp_path)["model_path"] == str(receipt)
    assert read_settings(tmp_path)["always_resident"] is True
    assert sorted(p.name for p in model.iterdir()) == ["config.json", "model.bin", "tokenizer.json"]
    receipt.write_text(json.dumps({"schema": "unknown", "model": "turbo", "path": str(model)}), encoding="utf-8")
    with pytest.raises(ModelUnavailable, match="receipt"):
        model_directory(str(receipt))


def test_missing_helene_receipt_defaults_to_own_configurable_skill_state(tmp_path, monkeypatch):
    import whisper_runtime

    monkeypatch.setattr(whisper_runtime, "DEFAULT_RECEIPT", str(tmp_path / "not-installed.json"))
    choice = read_settings(tmp_path)
    assert choice == {"model_path": str((tmp_path / "models").resolve()), "always_resident": False}
    with pytest.raises(ModelUnavailable, match="missing"):
        model_directory(choice["model_path"])
    # A missing model is an actionable status, not a broken Settings form.
    write_settings(tmp_path, validate_choice(str(make_model(tmp_path)), False))
    assert model_directory(read_settings(tmp_path)["model_path"]).is_dir()


def test_settings_and_audio_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        validate_choice("https://example.com/model", False)
    with pytest.raises(ValueError):
        validate_choice("relative", False)
    with pytest.raises(ValueError):
        validate_choice(str(tmp_path), "false")
    (tmp_path / "voice_settings.json").write_text('{"model_path":"relative"}', encoding="utf-8")
    with pytest.raises(ModelUnavailable, match="invalid"):
        read_settings(tmp_path)
    audio_format("audio/webm;codecs=opus", b"\x1a\x45\xdf\xa3foo")
    audio_format("audio/mpeg", b"ID3\x04\x00\x00")
    audio_format("audio/mpeg", b"\xff\xfb\x90\x00")
    with pytest.raises(ValueError, match="signature"):
        audio_format("audio/ogg", b"\x1a\x45\xdf\xa3foo")
    with pytest.raises(ValueError, match="signature"):
        audio_format("audio/mpeg", b"OggSwrong-container")


def test_byte_limits_hold_in_both_directions_for_each_path():
    """The dictation ceiling is core's 512 KiB; the attachment ceiling is a connector's 20 MiB."""
    assert (DICTATION_MAX_BYTES, DICTATION_MAX_SECONDS) == (524288, 120)
    assert (ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_SECONDS) == (20 * 1024 * 1024, 600)
    head = b"\x1a\x45\xdf\xa3"
    audio_format("audio/webm", head + b"x" * (DICTATION_MAX_BYTES - 4))
    with pytest.raises(ValueError, match="1..524288 bytes"):
        audio_format("audio/webm", head + b"x" * (DICTATION_MAX_BYTES - 3))
    # What dictation refuses, an attached recording may be, up to its own cap.
    audio_format("audio/webm", head + b"x" * (DICTATION_MAX_BYTES - 3), ATTACHMENT_MAX_BYTES)
    audio_format("audio/webm", head + b"x" * (ATTACHMENT_MAX_BYTES - 4), ATTACHMENT_MAX_BYTES)
    with pytest.raises(ValueError, match="1..20971520 bytes"):
        audio_format("audio/webm", head + b"x" * (ATTACHMENT_MAX_BYTES - 3), ATTACHMENT_MAX_BYTES)


def test_parallel_settings_writes_have_distinct_atomic_temp_files(tmp_path, monkeypatch):
    """The download thread and Settings POST share a PID, but not a temp name."""
    import whisper_runtime

    original_replace = whisper_runtime.os.replace
    temp_names = []
    failures = []
    in_replace = threading.Event()

    def concurrent_replace(source, target):
        temp_names.append(Path(source).name)
        in_replace.set()
        time.sleep(0.05)  # Give the second writer time to contend.
        original_replace(source, target)

    monkeypatch.setattr(whisper_runtime.os, "replace", concurrent_replace)

    def save(name):
        try:
            write_settings(tmp_path, validate_choice(str(tmp_path / name), False))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=save, args=(name,)) for name in ("small", "turbo")]
    threads[0].start()
    assert in_replace.wait(timeout=3)
    threads[1].start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert not failures
    assert len(temp_names) == len(set(temp_names)) == 2
    assert read_settings(tmp_path)["model_path"] in {str(tmp_path / "small"), str(tmp_path / "turbo")}
    assert not list(tmp_path.glob("voice_settings.*.tmp"))


def test_download_stays_in_skill_state_and_later_owner_selection_wins(tmp_path, monkeypatch):
    """No network: emulate the HF API at the actual companion call boundary."""
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("voice_companion_test", ROOT / "scripts" / "companion.py")
    companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(companion)
    original = make_model(tmp_path)
    downloaded = tmp_path / "models" / "snapshot"
    downloaded.parent.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        downloaded.mkdir(exist_ok=True)
        (downloaded / name).write_bytes(b"valid")
    write_settings(tmp_path, validate_choice(str(original), False))
    entered, release = threading.Event(), threading.Event()

    def fake_download(*, repo_id, cache_dir, allow_patterns):
        assert repo_id == companion.MODEL_REPOS["small"]
        assert cache_dir == str(tmp_path / "models")
        assert "model.bin" in allow_patterns
        entered.set()
        assert release.wait(timeout=60)  # an upper bound for a starved runner, not a delay
        return str(downloaded)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=fake_download))
    engine = companion.Engine()
    engine.install("small")
    assert entered.wait(timeout=60)
    chosen = tmp_path / "newer-choice"
    chosen.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (chosen / name).write_bytes(b"valid")
    engine.configure(validate_choice(str(chosen), False))
    release.set()
    deadline = time.monotonic() + 60
    while engine.installing and time.monotonic() < deadline:
        time.sleep(.01)
    assert not engine.installing and not engine.install_error
    assert "newer selection was kept" in engine.install_notice
    assert read_settings(tmp_path)["model_path"] == str(chosen)

    engine.install("small")
    deadline = time.monotonic() + 60
    while engine.installing and time.monotonic() < deadline:
        time.sleep(.01)
    assert not engine.installing and not engine.install_error
    assert read_settings(tmp_path)["model_path"] == str(downloaded)


def request(port, token, method, path, payload=b"", mime="application/json", timeout=3):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, payload, {"X-Voice-Token": token, "Content-Type": mime})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_supervised_companion_loopback_auth_and_readiness(tmp_path):
    model = make_model(tmp_path)
    env = {**os.environ, "OUROBOROS_SKILL_STATE_DIR": str(tmp_path)}
    # Windows venv python.exe is a launcher: Popen.pid is not the interpreter.
    executable = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    child = subprocess.Popen([executable, str(ROOT / "scripts" / "companion.py")],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        endpoint = tmp_path / "voice_endpoint.json"
        for _ in range(60):
            if endpoint.exists():
                break
            if child.poll() is not None:
                raise AssertionError(child.stderr.read().decode("utf-8", "replace"))
            time.sleep(0.05)
        data = json.loads(endpoint.read_text(encoding="utf-8"))
        assert data["pid"] == child.pid
        port, token = data["port"], data["token"]
        assert request(port, "wrong", "GET", "/status")[0] == 403
        status, empty = request(port, token, "GET", "/status")
        assert status == 200 and empty["resident"] is False
        # On Egor's host the default Hélène receipt is present; elsewhere it
        # may be unavailable. Both states are explicit and side-effect-free.
        assert isinstance(empty["ready"], bool)
        status, updated = request(port, token, "POST", "/settings", json.dumps({
            "model_path": str(model), "always_resident": False,
        }).encode())
        assert status == 200 and updated["model"] == str(model) and updated["resident"] is False
        # The test host may lack the skill's isolated native wheel closure.
        # A valid model alone must not claim inference readiness.
        if not updated["ready"]:
            assert "dependencies" in updated["reason"]
        # Settings' action renderer submits POST regardless of the declaration.
        # The registered POST handler must turn a read-only worker GET into
        # the shape that renderer actually projects, not report "Saved."
        from starlette.requests import Request
        plugin = load_plugin()
        status_handler = plugin._routes(tmp_path)[0]
        presented = asyncio.run(status_handler(Request({
            "type": "http", "method": "POST", "path": "/status", "headers": [],
        })))
        details = json.loads(presented.body)
        assert presented.status_code == 200
        assert details["voice"]["state"] == ("ready" if updated["ready"] else "unavailable")
        assert details["voice"]["message"]
        assert request(port, token, "POST", "/models/install", b'{"model":"arbitrary"}')[0] == 400
        assert request(port, token, "POST", "/transcribe", b"not audio", "audio/webm")[0] == 400
        status, selected = request(port, token, "POST", "/settings", json.dumps({
            "model_path": str(model), "always_resident": True,
        }).encode())
        assert status == 200 and selected["always_resident"] is True
        (model / "tokenizer.json").unlink()
        status, unavailable = request(port, token, "GET", "/status")
        assert status == 200 and unavailable["ready"] is False
        assert unavailable["model"] == str(model) and unavailable["always_resident"] is True
        assert unavailable["reason"]
    finally:
        child.terminate()
        child.wait(timeout=5)
        child.stderr.close()


@pytest.mark.parametrize("mime,audio", [(b"audio/webm", b"\x1a\x45\xdf\xa3fake-webm-audio"),
                                      (b"audio/mp4", b"\x00\x00\x00\x18ftypM4A fake-mp4")])
def test_real_plugin_route_forwards_raw_audio_over_private_loopback(tmp_path, mime, audio):
    from starlette.requests import Request

    plugin = load_plugin()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            count = int(self.headers['Content-Length'])
            self.server.observed = (self.headers.get('X-Voice-Token'), self.rfile.read(count))
            self.server.source = self.headers.get('X-Voice-Source')
            body = json.dumps({'text': 'recognized locally'}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Receiver) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (tmp_path / 'voice_endpoint.json').write_text(json.dumps({
            'port': server.server_port, 'token': 'x' * 32, 'pid': os.getpid(),
        }), encoding='utf-8')
        async def receive():
            return {'type': 'http.request', 'body': audio, 'more_body': False}
        request_obj = Request({'type': 'http', 'method': 'POST', 'path': '/transcribe',
                               'headers': [(b'content-type', mime), (b'x-voice-source', b'attachment'),
                                           (b'content-length', str(len(audio)).encode())]}, receive)
        _status, transcribe, _settings, _install = plugin._routes(tmp_path)
        response = asyncio.run(transcribe(request_obj))
        assert response.status_code == 200
        assert json.loads(response.body) == {'text': 'recognized locally'}
        assert server.observed == ('x' * 32, audio) and server.source == 'dictation'
        server.shutdown()
        thread.join(timeout=2)


def test_download_failure_is_visible_even_when_model_is_still_missing():
    from starlette.responses import JSONResponse

    plugin = load_plugin()
    response = plugin._settings_response(JSONResponse({
        "ready": False, "reason": "selected local model directory is missing",
        "install_error": "network download failed", "install_notice": "",
    }))
    visible = json.loads(response.body)
    assert visible["voice"] == {"state": "unavailable", "message": "network download failed"}


@pytest.mark.parametrize("mime", ["audio/mp4", 'audio/mp4;codecs="mp4a.40.2"'])
def test_mp4_signature_matches_only_at_offset_four(mime):
    audio_format(mime, b"\x00\x00\x00\x18ftypM4A ")
    for invalid in (b"ftypM4A ", b"\x00\x00\x00ftypM4A ", b"OggSnot-mp4", b"123"):
        with pytest.raises(ValueError, match="signature"):
            audio_format(mime, invalid)


def test_mp4_decodes_through_pyav_before_local_transcription(tmp_path, monkeypatch):
    import io

    av = _runtime("av")  # the skill's isolated runtime installs PyAV; hosted CI requires it
    np = _runtime("numpy")

    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("voice_mp4_test", ROOT / "scripts" / "companion.py")
    companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(companion)
    write_settings(tmp_path, validate_choice(str(make_model(tmp_path)), False))
    output = io.BytesIO()
    with av.open(output, "w", format="mp4") as container:
        stream = container.add_stream("aac", rate=16000)
        frame = av.AudioFrame.from_ndarray(np.zeros((1, 4096), dtype=np.float32), format="fltp", layout="mono")
        frame.sample_rate = 16000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    class Model:
        def __init__(self, path, **kwargs):
            assert path == str(tmp_path / "local-model")
            assert kwargs["local_files_only"] is True

        def transcribe(self, audio, **kwargs):
            assert audio.dtype == np.float32 and 1600 <= len(audio) < 16000
            return [types.SimpleNamespace(text=" Safari dictation ")], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    engine = companion.Engine()
    cold, warm = {}, {}
    assert engine.transcribe(output.getvalue(), "audio/mp4", cold) == "Safari dictation"
    first = engine.model
    # Saving the same snapshot or just residency must not reload weights.
    engine.configure(validate_choice(str(tmp_path / "local-model"), True))
    assert engine.model is first
    assert engine.transcribe(output.getvalue(), "audio/mp4", warm) == "Safari dictation"
    assert engine.model is first
    assert cold["model_loaded"] is True and warm["model_loaded"] is False
    stages = ("imports_ms", "decode_ms", "lock_wait_ms", "selection_and_load_ms", "inference_ms")
    for metrics in (cold, warm):
        assert all(metrics[key] >= 0 for key in stages)
        assert abs(sum(metrics[key] for key in stages) - metrics["worker_total_ms"]) < .01
    assert "Safari" not in json.dumps(cold)


@pytest.mark.parametrize("resident", [False, True])
def test_configure_retains_same_resolved_snapshot(tmp_path, monkeypatch, resident):
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("voice_config_test", ROOT / "scripts" / "companion.py")
    companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(companion)
    model = make_model(tmp_path)
    receipt = tmp_path / "installed.json"
    receipt.write_text(json.dumps({"schema": "helene.voice.v1", "model": "turbo", "path": str(model)}), encoding="utf-8")
    engine = companion.Engine()
    original = object()
    engine.model, engine.loaded_path, engine.last_use = original, str(model.resolve()), 123.0
    engine.configure(validate_choice(str(receipt), resident))
    assert engine.model is original and engine.last_use == 123.0
    engine.configure(validate_choice(str(model), resident))
    assert engine.model is original
    other = tmp_path / "other"
    other.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (other / name).write_bytes(b"valid")
    engine.configure(validate_choice(str(other), resident))
    assert engine.model is None and engine.loaded_path == ""
    # A failed selection must preserve both saved settings and residency.
    engine.model, engine.loaded_path = original, str(other.resolve())
    with pytest.raises(ModelUnavailable):
        engine.configure(validate_choice(str(tmp_path / "missing"), resident))
    assert engine.model is original
    assert read_settings(tmp_path)["model_path"] == str(other)


def test_busy_rejected_before_decode_and_exception_releases_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("voice_busy_test", ROOT / "scripts" / "companion.py")
    companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(companion)
    engine = companion.Engine()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def held(*args):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return 'finished'
    monkeypatch.setattr(engine, '_transcribe', held)
    thread = threading.Thread(target=engine.transcribe, args=(b'OggS', 'audio/ogg'))
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(companion.VoiceBusy):
            engine.transcribe(b'OggS', 'audio/ogg')
        assert calls == [1]
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    def fail(*args):
        raise ValueError('decode failure')
    monkeypatch.setattr(engine, '_transcribe', fail)
    with pytest.raises(ValueError, match='decode failure'):
        engine.transcribe(b'OggS', 'audio/ogg')
    monkeypatch.setattr(engine, '_transcribe', lambda *args: 'next request')
    assert engine.transcribe(b'OggS', 'audio/ogg') == 'next request'


def _runtime(name):
    """PyAV and NumPy come from the skill's isolated install. Hosted CI sets WHISPER_REQUIRE_RUNTIME=1, so their
    absence fails there; a bare local checkout skips instead."""
    if os.environ.get("WHISPER_REQUIRE_RUNTIME"):
        return importlib.import_module(name)
    return pytest.importorskip(name)


def _mp4(seconds, rate=16000, bit_rate=None, noise=False):
    """A real AAC/MP4 clip at the given sample rate (seconds may be fractional); silent unless noise (big enough to
    cross a byte cap)."""
    import io

    av, np = _runtime("av"), _runtime("numpy")
    rng = np.random.default_rng(7)
    output = io.BytesIO()
    remaining = int(seconds * rate)
    with av.open(output, "w", format="mp4") as container:
        stream = container.add_stream("aac", rate=rate)
        if bit_rate:
            stream.bit_rate = bit_rate
        while remaining > 0:
            count = min(rate, remaining)
            remaining -= count
            data = (rng.uniform(-.3, .3, (1, count)) if noise else np.zeros((1, count))).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(data, format="fltp", layout="mono")
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


def _companion_module(tmp_path, monkeypatch, name):
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "companion.py")
    companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(companion)
    write_settings(tmp_path, validate_choice(str(make_model(tmp_path)), False))
    return companion


def _fake_whisper(monkeypatch, words="long words", seen=None):
    class Model:
        def __init__(self, path, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            if seen is not None:
                seen.append(len(audio))
            return [types.SimpleNamespace(text=words)], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
@pytest.mark.parametrize("source,accepted,over", [("dictation", (119, 120.3), 130), ("attachment", (599, 601), 610)])
def test_decoded_length_limit_holds_in_both_directions_per_source(tmp_path, monkeypatch, source, accepted, over, rate):
    """Native 16 kHz and 44.1/48 kHz clips (which pass through the resampler) around each path's length limit. The
    nominal limits are 120 and 600 s; the worker tolerates DURATION_GRACE_SECONDS more (a recorder that stops 300 ms
    late, codec padding), so a 120.3 s dictation is taken and one well over is refused."""
    companion = _companion_module(tmp_path, monkeypatch, "voice_length_test")
    seen = []
    _fake_whisper(monkeypatch, seen=seen)
    engine = companion.Engine()
    nominal = {"dictation": DICTATION_MAX_SECONDS, "attachment": ATTACHMENT_MAX_SECONDS}[source]
    limit = nominal + DURATION_GRACE_SECONDS
    assert limit == {"dictation": 125, "attachment": 605}[source]
    for seconds in accepted:
        seen.clear()
        assert engine.transcribe(_mp4(seconds, rate), "audio/mp4", None, source) == "long words"
        assert seen and abs(seen[0] - seconds * 16000) < 16000 // 2
    seen.clear()
    with pytest.raises(ValueError, match=f"audio longer than {limit} seconds"):
        engine.transcribe(_mp4(over, rate), "audio/mp4", None, source)
    assert seen == []  # refused while decoding, before the model loads or runs
    if source == "dictation":
        # A recording that the attachment path takes is still too long for dictation; unknown means dictation.
        assert engine.transcribe(_mp4(over, rate), "audio/mp4", None, "attachment") == "long words"
        with pytest.raises(ValueError, match="audio longer than 125 seconds"):
            engine.transcribe(_mp4(over, rate), "audio/mp4", None, "unknown")


@pytest.mark.parametrize("drained,accepted", [(0, True), (1, False)])
def test_length_limit_also_holds_for_what_the_resampler_drains_at_the_end(tmp_path, monkeypatch, drained, accepted):
    """The last samples come out of resample(None): a clip exactly at the limit passes, one sample over is refused."""
    np = _runtime("numpy")
    companion = _companion_module(tmp_path, monkeypatch, "voice_drain_test")
    _fake_whisper(monkeypatch)
    limit = (DICTATION_MAX_SECONDS + DURATION_GRACE_SECONDS) * 16000

    class Converted:
        def __init__(self, count):
            self.count = count

        def to_ndarray(self):
            return np.zeros((1, self.count), dtype=np.float32)

    class Resampler:
        def __init__(self, **kwargs):
            pass

        def resample(self, frame):
            return [Converted(limit)] if frame is not None else ([Converted(drained)] if drained else [])

    class Container:
        streams = types.SimpleNamespace(audio=[object()])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def decode(self, stream):
            return [object()]

    fake_av = types.SimpleNamespace(open=lambda *a, **k: Container(),
                                    audio=types.SimpleNamespace(resampler=types.SimpleNamespace(AudioResampler=Resampler)))
    monkeypatch.setitem(sys.modules, "av", fake_av)
    engine = companion.Engine()
    if accepted:
        assert engine.transcribe(b"OggS", "audio/ogg", None, "dictation") == "long words"
    else:
        with pytest.raises(ValueError, match="audio longer than 125 seconds"):
            engine.transcribe(b"OggS", "audio/ogg", None, "dictation")


@pytest.mark.parametrize("char", ["я", "😀"])  # 2 and 4 bytes in UTF-8
def test_transcript_cap_is_exact_in_both_directions(tmp_path, monkeypatch, char):
    import whisper_runtime

    _runtime("av")
    companion = _companion_module(tmp_path, monkeypatch, "voice_transcript_test")
    clip = _mp4(2)
    for count, accepted in ((whisper_runtime.MAX_TRANSCRIPT_CHARS, True), (whisper_runtime.MAX_TRANSCRIPT_CHARS + 1, False)):
        _fake_whisper(monkeypatch, words=char * count)
        engine = companion.Engine()
        if accepted:
            assert engine.transcribe(clip, "audio/mp4") == char * count
        else:
            with pytest.raises(ValueError, match="transcript too long"):
                engine.transcribe(clip, "audio/mp4")
    # The reply bound of the plugin's reader holds the longest accepted transcript at 4 bytes a character.
    longest = json.dumps({"text": "😀" * whisper_runtime.MAX_TRANSCRIPT_CHARS, "timings": {}}, ensure_ascii=False)
    assert len(longest.encode("utf-8")) <= whisper_runtime.MAX_REPLY_BYTES


@pytest.mark.parametrize("trailing,accepted", [(b"", True), (b"\n", False)])
def test_plugin_reply_reader_takes_a_complete_reply_of_exactly_its_bound_and_refuses_anything_after_it(tmp_path, trailing, accepted):
    """A complete JSON reply of exactly MAX_REPLY_BYTES parses; one trailing byte (whitespace keeps the JSON
    valid, so only the reader's own guard can refuse it) does not."""
    import whisper_runtime

    plugin = load_plugin()
    frame = len(json.dumps({"text": ""}).encode())
    body = json.dumps({"text": "x" * (whisper_runtime.MAX_REPLY_BYTES - frame)}).encode()
    assert len(body) == whisper_runtime.MAX_REPLY_BYTES
    body += trailing

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (tmp_path / "voice_endpoint.json").write_text(json.dumps({
            "port": server.server_port, "token": "x" * 32, "pid": os.getpid()}), encoding="utf-8")
        response = plugin._worker(tmp_path, "POST", "/transcribe", b"OggS", "audio/ogg")
        server.shutdown()
    assert (response.status_code == 200) is accepted
    if not accepted:
        assert response.status_code == 503 and json.loads(response.body)["error"]["code"] == "worker_unavailable"


def test_waits_are_per_route_and_the_tool_outlasts_its_worker_wait():
    import whisper_runtime as rt

    assert rt.wait_for("/status", "dictation") == rt.wait_for("/settings", "attachment") == rt.CONTROL_WAIT_SECONDS == 30
    assert rt.wait_for("/models/install", "dictation") == rt.CONTROL_WAIT_SECONDS
    assert rt.wait_for("/transcribe", "dictation") == rt.DICTATION_WAIT_SECONDS == 125
    assert rt.wait_for("/transcribe", "attachment") == rt.ATTACHMENT_WAIT_SECONDS >= 2 * ATTACHMENT_MAX_SECONDS
    assert rt.wait_for("/transcribe", "unknown") == rt.DICTATION_WAIT_SECONDS
    assert rt.TOOL_TIMEOUT_SECONDS > rt.ATTACHMENT_WAIT_SECONDS


class _Child:
    """A real companion process with a fake faster_whisper (no weights) and the real av/numpy decoder. Every spawned
    process is recorded in `spawned`, and a constructor that fails for any reason terminates its own child first."""

    spawned: list = []
    INIT_DEADLINE_SECONDS = 30

    def __init__(self, tmp_path, chars=5, char="я", extra_env=None):
        self.proc = None
        try:
            fake = tmp_path / "fake_site"
            fake.mkdir(exist_ok=True)
            (fake / "faster_whisper.py").write_text(
                "import os, types\nclass WhisperModel:\n    def __init__(self, path, **kw):\n        pass\n"
                "    def transcribe(self, audio, **kw):\n        import time\n"
                "        time.sleep(float(os.environ.get('FAKE_SECONDS', '0')))\n"
                "        return [types.SimpleNamespace(text=os.environ['FAKE_CHAR'] * int(os.environ['FAKE_CHARS']))], None\n",
                encoding="utf-8")
            env = {**os.environ, "OUROBOROS_SKILL_STATE_DIR": str(tmp_path), "FAKE_CHARS": str(chars), "FAKE_CHAR": char,
                   "PYTHONPATH": os.pathsep.join([str(fake), os.environ.get("PYTHONPATH", "")]), **(extra_env or {})}
            self.proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "companion.py")], env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _Child.spawned.append(self.proc)
            endpoint = tmp_path / "voice_endpoint.json"
            deadline = time.monotonic() + self.INIT_DEADLINE_SECONDS
            while not endpoint.exists():
                if self.proc.poll() is not None:
                    raise AssertionError(self.proc.stderr.read().decode("utf-8", "replace"))
                if time.monotonic() > deadline:
                    raise AssertionError("companion did not publish its endpoint in time")
                time.sleep(0.05)
            data = json.loads(endpoint.read_text(encoding="utf-8"))
            self.port, self.token = data["port"], data["token"]
            # Initialisation has one deadline end to end: the settings call gets what is left of it.
            request(self.port, self.token, "POST", "/settings", json.dumps({
                "model_path": str(make_model(tmp_path)), "always_resident": False}).encode(),
                timeout=max(1.0, deadline - time.monotonic()))
        except BaseException:
            self.close()
            raise

    def post(self, body, mime="audio/mp4", source=None, path="/transcribe", timeout=60):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        headers = {"X-Voice-Token": self.token, "Content-Type": mime}
        if source:
            headers["X-Voice-Source"] = source
        try:
            conn.request("POST", path, body, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def open_upload(self, size, source="attachment", mime="audio/ogg", send=0):
        """Start an upload of `size` bytes and send only `send` of them; returns the live connection."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest("POST", "/transcribe")
        for name, value in {"X-Voice-Token": self.token, "Content-Type": mime, "X-Voice-Source": source,
                            "Content-Length": str(size)}.items():
            conn.putheader(name, value)
        conn.endheaders()
        if send:
            conn.send(b"OggS" + bytes(send - 4))
        return conn

    def close(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()  # this exact child only
            proc.wait(timeout=5)
        finally:
            if proc.stderr:
                proc.stderr.close()


def test_a_failed_fixture_constructor_leaves_no_companion_alive(tmp_path, monkeypatch):
    spawned_before = len(_Child.spawned)

    def boom(*args, **kwargs):
        raise RuntimeError("forced constructor failure")

    monkeypatch.setattr(sys.modules[__name__], "request", boom)
    with pytest.raises(RuntimeError, match="forced constructor failure"):
        _Child(tmp_path)
    mine = _Child.spawned[spawned_before:]
    assert len(mine) == 1 and mine[0].poll() is not None  # the child it spawned is gone


def test_source_reaches_the_real_decoder_through_the_plugin_and_the_companion_over_http(tmp_path):
    """One 130 s, ~2 MB clip crosses BOTH dictation limits: only the attachment source may carry it, and the
    source set by the skill's own caller is what the companion's decoder applies."""
    clip = _mp4(130, rate=44100, bit_rate=128000, noise=True)
    assert len(clip) > DICTATION_MAX_BYTES and len(clip) < ATTACHMENT_MAX_BYTES
    plugin = load_plugin()
    child = _Child(tmp_path)
    try:
        # Attachment: the same bytes pass both limits and reach the model.
        taken = plugin._worker(tmp_path, "POST", "/transcribe", clip, "audio/mp4", source="attachment")
        assert taken.status_code == 200 and json.loads(taken.body)["text"] == "яяяяя"
        # A 130 s clip that fits 512 KiB is refused on length as dictation and taken as attachment.
        short = _mp4(130, rate=44100)
        assert len(short) < DICTATION_MAX_BYTES
        over = plugin._worker(tmp_path, "POST", "/transcribe", short, "audio/mp4")
        assert over.status_code == 400 and "audio longer than 125 seconds" in json.loads(over.body)["error"]["message"]
        assert plugin._worker(tmp_path, "POST", "/transcribe", short, "audio/mp4", source="attachment").status_code == 200
    finally:
        child.close()


@pytest.mark.parametrize("char,count,status", [("я", None, 200), ("😀", None, 200), ("я", "over", 400)])
def test_boundary_sized_transcript_crosses_the_real_companion_and_plugin(tmp_path, char, count, status):
    import whisper_runtime

    chars = whisper_runtime.MAX_TRANSCRIPT_CHARS + (1 if count == "over" else 0)
    plugin = load_plugin()
    child = _Child(tmp_path, chars=chars, char=char)
    try:
        response = plugin._worker(tmp_path, "POST", "/transcribe", _mp4(2), "audio/mp4")
        assert response.status_code == status
        value = json.loads(response.body)
        if status == 200:
            assert value["text"] == char * chars
        else:
            assert "transcript too long" in value["error"]["message"]
    finally:
        child.close()


def _running(child, tmp_path, seconds=6):
    """Start one attached note whose (fake) recognition takes `seconds`; returns its thread and result list."""
    results = []
    thread = threading.Thread(target=lambda: results.append(child.post(_mp4(3), source="attachment")))
    thread.start()
    for _ in range(200):
        if request(child.port, child.token, "GET", "/status")[1]["busy"]:
            return thread, results
        time.sleep(0.05)
    raise AssertionError("recognition never started")


def test_status_answers_at_once_and_a_settings_save_during_recognition_is_refused_with_nothing_changed(tmp_path):
    plugin = load_plugin()
    child = _Child(tmp_path, extra_env={"FAKE_SECONDS": "6"})
    old = json.loads((tmp_path / "voice_settings.json").read_text(encoding="utf-8"))["model_path"]
    other = tmp_path / "other-model"
    other.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (other / name).write_bytes(b"valid")
    thread, results = _running(child, tmp_path)
    try:
        began = time.monotonic()
        status, snapshot = request(child.port, child.token, "GET", "/status")
        assert status == 200 and snapshot["busy"] is True and time.monotonic() - began < 2
        assert json.loads(plugin._worker(tmp_path, "GET", "/status").body)["busy"] is True
        # A save returns 503 AT ONCE (never waits on the model lock) and changes nothing, now or later.
        began = time.monotonic()
        status, body = request(child.port, child.token, "POST", "/settings", json.dumps(
            {"model_path": str(other), "always_resident": True}).encode())
        assert status == 503 and body["error"]["code"] == "voice_busy" and time.monotonic() - began < 2
        assert request(child.port, child.token, "POST", "/models/install", b'{"model":"small"}')[1]["error"]["code"] == "voice_busy"
        shown = plugin._settings_response(plugin._worker(tmp_path, "GET", "/status"))
        assert "a transcription is running" in json.loads(shown.body)["voice"]["message"]
    finally:
        thread.join(30)
    try:
        assert results and results[0][0] == 200
        # The recognition has ended: the refused save did NOT take effect afterwards, and the old model is selected.
        status, now = request(child.port, child.token, "GET", "/status")
        assert now["busy"] is False and now["model"] == old and now["always_resident"] is False
        assert json.loads((tmp_path / "voice_settings.json").read_text(encoding="utf-8"))["model_path"] == old
        # Idle: the same save applies and answers success.
        status, saved = request(child.port, child.token, "POST", "/settings", json.dumps(
            {"model_path": str(other), "always_resident": True}).encode())
        assert status == 200 and saved["model"] == str(other) and saved["always_resident"] is True
    finally:
        child.close()


def test_the_slot_is_reserved_before_the_body_and_a_second_or_oversized_upload_is_answered_unread(tmp_path):
    child = _Child(tmp_path)
    try:
        _slot_reservation_case(child, tmp_path)
    finally:
        child.close()


def _slot_reservation_case(child, tmp_path):
    first = child.open_upload(64 * 1024, send=1024)          # admitted: its body is still arriving
    try:
        time.sleep(0.5)
        assert request(child.port, child.token, "GET", "/status")[1]["busy"] is True
        second = child.open_upload(64 * 1024)                 # nothing of its body is ever sent
        response = second.getresponse()
        assert response.status == 503 and json.loads(response.read())["error"]["code"] == "voice_busy"
        second.close()
        # An over-limit Content-Length is refused 413 from its header alone, admitted or not.
        over = child.open_upload(DICTATION_MAX_BYTES + 1, source="dictation")
        response = over.getresponse()
        assert response.status == 413
        over.close()
        # The plugin's client turns an upload the worker refuses early into voice_busy as well.
        plugin = load_plugin()
        busy = plugin._worker(tmp_path, "POST", "/transcribe", b"OggS" + bytes(8 * 1024 * 1024), "audio/ogg", source="attachment")
        assert busy.status_code == 503 and json.loads(busy.body)["error"]["code"] == "voice_busy"
    finally:
        first.close()  # the sender gives up: the handler ends and frees the slot
    try:
        for _ in range(100):
            if not request(child.port, child.token, "GET", "/status")[1]["busy"]:
                break
            time.sleep(0.05)
        assert request(child.port, child.token, "GET", "/status")[1]["busy"] is False
        assert child.post(_mp4(2), source="attachment")[0] == 200
    finally:
        pass


def test_a_stalled_sender_loses_the_slot_after_the_upload_idle_deadline(tmp_path, monkeypatch):
    companion = _companion_module(tmp_path, monkeypatch, "voice_stall_test")
    monkeypatch.setattr(companion.Handler, "timeout", 1)  # the production value is UPLOAD_IDLE_SECONDS (30)
    server = ThreadingHTTPServer(("127.0.0.1", 0), companion.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.putrequest("POST", "/transcribe")
        for name, value in {"X-Voice-Token": companion.TOKEN, "Content-Type": "audio/ogg",
                            "X-Voice-Source": "attachment", "Content-Length": "100000"}.items():
            conn.putheader(name, value)
        conn.endheaders()
        conn.send(b"OggS")
        for _ in range(50):
            if companion.ENGINE.transcription.locked():
                break
            time.sleep(0.02)
        assert companion.ENGINE.transcription.locked()          # admitted, then stalls
        for _ in range(150):                                     # the idle deadline ends it and frees the slot
            if not companion.ENGINE.transcription.locked():
                break
            time.sleep(0.05)
        assert not companion.ENGINE.transcription.locked()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_companion_checks_each_sources_byte_limit_before_reading(tmp_path):
    child = _Child(tmp_path)
    try:
        def post(size, source=None):
            conn = http.client.HTTPConnection("127.0.0.1", child.port, timeout=5)
            headers = {"X-Voice-Token": child.token, "Content-Type": "audio/ogg"}
            if source:
                headers["X-Voice-Source"] = source
            try:
                # Announce the length, then send the body only when the cap should let it through: a refusal
                # is answered from the header, before the companion reads a byte.
                conn.putrequest("POST", "/transcribe")
                for name, value in {**headers, "Content-Length": str(size)}.items():
                    conn.putheader(name, value)
                conn.endheaders()
                if size <= (ATTACHMENT_MAX_BYTES if source == "attachment" else DICTATION_MAX_BYTES):
                    conn.send(b"OggS" + b"\0" * (size - 4))
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        assert post(DICTATION_MAX_BYTES + 1)[0] == 413          # dictation (the default) stops at 512 KiB
        assert post(DICTATION_MAX_BYTES + 1, "dictation")[0] == 413
        assert post(ATTACHMENT_MAX_BYTES + 1, "attachment")[0] == 413
        # An attachment larger than dictation's cap passes the byte guard: the refusal is about the (not
        # decodable) audio, never about size.
        status, body = post(DICTATION_MAX_BYTES + 1, "attachment")
        assert status == 400 and "audio must contain" not in body["error"]["message"]
        status, body = post(DICTATION_MAX_BYTES)
        assert status == 400 and "audio must contain" not in body["error"]["message"]
    finally:
        child.close()


def test_plugin_texts_and_manifest_state_the_same_limits(tmp_path):
    import yaml

    plugin = load_plugin()
    tools, widgets = {}, []

    class Api:
        def get_state_dir(self):
            return str(tmp_path)

        def register_companion_process(self, name):
            pass

        def register_tool(self, name, **kwargs):
            tools[name] = kwargs

        def register_route(self, *args, **kwargs):
            pass

        def register_settings_section(self, name, **kwargs):
            widgets.append(kwargs["schema"]["components"][0]["text"])

    plugin.register(Api())
    row = yaml.safe_load((ROOT / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1])["contributes"][0]
    assert (row["max_bytes"], row["max_seconds"]) == (DICTATION_MAX_BYTES, DICTATION_MAX_SECONDS)
    description = tools["transcribe_attachment"]["description"]
    assert "Max 20 MiB / 10 minutes" in description and "512 KiB / 2 minutes" in description
    assert "384" not in description and "60 seconds" not in description
    assert "2 minutes and 512 KiB" in widgets[0] and "10 minutes and 20 MiB" in widgets[0]
    import whisper_runtime
    assert tools["transcribe_attachment"]["timeout_sec"] == whisper_runtime.TOOL_TIMEOUT_SECONDS > ATTACHMENT_MAX_SECONDS * 2


def test_real_plugin_route_refuses_over_the_dictation_ceiling_and_takes_exactly_it(tmp_path):
    from starlette.requests import Request

    plugin = load_plugin()
    seen = []
    sources = []
    plugin._worker = lambda *args, **kwargs: (seen.append(len(args[3])), sources.append(kwargs.get("source", "dictation"))) and plugin.JSONResponse({"text": "ok"})
    audio = b"OggS" + b"\0" * (DICTATION_MAX_BYTES - 4)

    def call(body, length):
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        scope = {"type": "http", "method": "POST", "path": "/transcribe",
                 # A browser caller claiming the attachment path: the skill ignores it, the route sets its own source.
                 "headers": [(b"content-type", b"audio/ogg"), (b"x-voice-source", b"attachment"),
                             (b"content-length", str(length).encode())]}
        return asyncio.run(plugin._routes(tmp_path)[1](Request(scope, receive)))

    assert call(audio, len(audio)).status_code == 200 and seen == [DICTATION_MAX_BYTES]
    refused = call(audio + b"\0", len(audio) + 1)
    assert refused.status_code == 413 and "524288" in json.loads(refused.body)["error"]["message"]
    assert seen == [DICTATION_MAX_BYTES] and sources == ["dictation"]  # refused one never reached the worker


def test_a_busy_save_answers_busy_before_the_model_directory_is_read(tmp_path):
    child = _Child(tmp_path, extra_env={"FAKE_SECONDS": "4"})
    thread, results = _running(child, tmp_path)
    try:
        missing = tmp_path / "no-such-snapshot"          # would be a 400 if it were read first
        status, body = request(child.port, child.token, "POST", "/settings", json.dumps(
            {"model_path": str(missing), "always_resident": False}).encode())
        assert status == 503 and body["error"]["code"] == "voice_busy"
    finally:
        thread.join(30)
    try:
        status, body = request(child.port, child.token, "POST", "/settings", json.dumps(
            {"model_path": str(missing), "always_resident": False}).encode())
        assert status == 400  # idle: the missing snapshot is now reported, and nothing was changed either way
    finally:
        child.close()


@pytest.mark.parametrize("dies_after_request", [True, False])
def test_worker_that_disconnects_is_one_typed_503_and_never_a_second_getresponse(tmp_path, dies_after_request):
    """A socket-level worker that accepts the request and closes without replying (a restart) gives 503
    worker_unavailable; one that closes while the body is still being sent gives voice_busy."""
    import socket

    plugin = load_plugin()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    (tmp_path / "voice_endpoint.json").write_text(json.dumps({
        "port": listener.getsockname()[1], "token": "x" * 32, "pid": os.getpid()}), encoding="utf-8")

    def serve():
        conn, _ = listener.accept()
        if dies_after_request:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            body_len = int(next(l for l in data.split(b"\r\n") if l.lower().startswith(b"content-length")).split(b":")[1])
            have = len(data.split(b"\r\n\r\n", 1)[1])
            while have < body_len:
                have += len(conn.recv(65536))
        else:
            conn.recv(1024)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")  # RST
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        body = b"OggS" + bytes(16 * 1024 * 1024) if not dies_after_request else b"OggS" + bytes(1000)
        response = plugin._worker(tmp_path, "POST", "/transcribe", body, "audio/ogg", source="attachment")
        value = json.loads(response.body)
        assert response.status_code == 503
        assert value["error"]["code"] == ("worker_unavailable" if dies_after_request else "voice_busy")
    finally:
        thread.join(10)
        listener.close()
