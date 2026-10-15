"""Cheap offline tests: no model download, CTranslate load or live installation."""
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
    MAX_AUDIO_BYTES,
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
    with pytest.raises(ValueError, match="393216"):
        audio_format("audio/webm", b"\x1a\x45\xdf\xa3" + b"x" * MAX_AUDIO_BYTES)


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
        assert release.wait(timeout=3)
        return str(downloaded)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=fake_download))
    engine = companion.Engine()
    engine.install("small")
    assert entered.wait(timeout=3)
    chosen = tmp_path / "newer-choice"
    chosen.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (chosen / name).write_bytes(b"valid")
    engine.configure(validate_choice(str(chosen), False))
    release.set()
    for _ in range(100):
        if not engine.installing:
            break
        time.sleep(.01)
    assert not engine.installing and not engine.install_error
    assert "newer selection was kept" in engine.install_notice
    assert read_settings(tmp_path)["model_path"] == str(chosen)

    engine.install("small")
    for _ in range(100):
        if not engine.installing:
            break
        time.sleep(.01)
    assert not engine.installing and not engine.install_error
    assert read_settings(tmp_path)["model_path"] == str(downloaded)


def request(port, token, method, path, payload=b"", mime="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request(method, path, payload, {"X-Voice-Token": token, "Content-Type": mime})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_supervised_companion_loopback_auth_and_readiness(tmp_path):
    model = make_model(tmp_path)
    env = {**os.environ, "OUROBOROS_SKILL_STATE_DIR": str(tmp_path)}
    child = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "companion.py")],
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


def test_real_plugin_route_forwards_raw_audio_over_private_loopback(tmp_path):
    from starlette.requests import Request

    plugin = load_plugin()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            count = int(self.headers['Content-Length'])
            self.server.observed = (self.headers.get('X-Voice-Token'), self.rfile.read(count))
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
        audio = b'\x1a\x45\xdf\xa3fake-webm-audio'
        async def receive():
            return {'type': 'http.request', 'body': audio, 'more_body': False}
        request_obj = Request({'type': 'http', 'method': 'POST', 'path': '/transcribe',
                               'headers': [(b'content-type', b'audio/webm'),
                                           (b'content-length', str(len(audio)).encode())]}, receive)
        _status, transcribe, _settings, _install = plugin._routes(tmp_path)
        response = asyncio.run(transcribe(request_obj))
        assert response.status_code == 200
        assert json.loads(response.body) == {'text': 'recognized locally'}
        assert server.observed == ('x' * 32, audio)
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
