"""Skill-supervised loopback Whisper worker. No cloud inference or host-runtime imports.

The extension handler is a short-lived isolated child; this companion alone owns
model residency. Its private rendezvous and token live under the skill state
root, and the host supervisor owns termination on disable/shutdown/Panic.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from whisper_runtime import (  # noqa: E402
    IDLE_SECONDS,
    MAX_TRANSCRIPT_CHARS,
    SOURCE_HEADER,
    UPLOAD_IDLE_SECONDS,
    audio_format,
    error,
    limits_for,
    model_directory,
    read_settings,
    validate_choice,
    write_settings,
)

STATE = Path(os.environ["OUROBOROS_SKILL_STATE_DIR"]).resolve()
ENDPOINT = STATE / "voice_endpoint.json"
TOKEN = secrets.token_urlsafe(32)
MODEL_REPOS = {
    "small": "Systran/faster-whisper-small",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class VoiceBusy(ValueError):
    pass


class Engine:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.transcription = threading.Lock()
        self.model = None
        self.loaded_path = ""
        self.last_use = 0.0
        self.installing = ""
        self.install_error = ""
        self.install_notice = ""

    def evict(self) -> None:
        while True:
            time.sleep(10)
            try:
                settings = read_settings(STATE)
                with self.lock:
                    if self.model is not None and not settings["always_resident"] \
                            and time.monotonic() - self.last_use >= IDLE_SECONDS:
                        self.model = None
                        self.loaded_path = ""
            except Exception:
                # An unreadable setting must not keep a large model resident.
                with self.lock:
                    self.model = None
                    self.loaded_path = ""

    def status(self) -> dict:
        settings = {"model_path": "", "always_resident": False}
        try:
            settings = read_settings(STATE)
            directory = model_directory(settings["model_path"])
            reason = ""
            missing = [name for name in ("av", "numpy", "faster_whisper")
                       if importlib.util.find_spec(name) is None]
            if missing:
                reason = "Isolated voice dependencies missing: " + ", ".join(missing)
        except Exception as exc:
            directory = None
            reason = str(exc)
        # A snapshot of plain attributes: status never waits for the model lock, which a recognition holds
        # for its whole run; `busy` tells the caller why Whisper cannot take another recording right now.
        return {
            "busy": self.transcription.locked(),
            "ready": directory is not None and not reason and not self.installing,
            "resident": self.model is not None,
            "model": str(directory or settings["model_path"]),
            "always_resident": settings["always_resident"],
            "idle_expiry_sec": 0 if settings["always_resident"] else IDLE_SECONDS,
            "installing": self.installing,
            "install_error": self.install_error,
            "install_notice": self.install_notice,
            "reason": reason or ("Model download in progress" if self.installing else ""),
        }

    def _control_lock(self):
        """The model lock for a settings change, never waited for: a recognition holds it for its whole run, and a
        save that waited could apply after the caller was told 503."""
        # A recognition owns the single slot from admission; it takes the model lock only after decoding, so the
        # lock alone would let a save through during the decode. Both are checked, neither is waited for.
        if self.transcription.locked() or not self.lock.acquire(blocking=False):
            raise VoiceBusy("A local transcription is already running; try again after it finishes")

    def configure(self, settings: dict) -> dict:
        path = str(model_directory(settings["model_path"]).resolve())
        self._control_lock()
        try:
            write_settings(STATE, settings)
            # Residency is a retention policy, not a new model selection.
            if self.loaded_path != path:
                self.model = None
                self.loaded_path = ""
            self.install_error = ""
            self.install_notice = ""
        finally:
            self.lock.release()
        return self.status()

    def transcribe(self, data: bytes, mime: str, timings: dict | None = None, source: str = "dictation",
                   admitted: bool = False) -> str:
        # Refuse excess work before imports/decode; never queue stale recordings. The HTTP handler reserves the
        # slot before it reads the body and passes admitted=True (it releases the slot itself).
        if not admitted and not self.transcription.acquire(blocking=False):
            raise VoiceBusy("A local transcription is already running; try again after it finishes")
        try:
            return self._transcribe(data, mime, timings, source)
        finally:
            if not admitted:
                self.transcription.release()

    def _transcribe(self, data: bytes, mime: str, timings: dict | None = None, source: str = "dictation") -> str:
        started = time.perf_counter()
        max_bytes, max_seconds = limits_for(source)
        audio_format(mime, data, max_bytes)
        # Decode BEFORE loading 1.6 GB of weights; cap decoded samples too.
        import av
        import numpy as np
        from faster_whisper import WhisperModel

        imported = time.perf_counter()
        parts = []
        count = 0
        # PyAV detects WebM/Ogg/MP3 and Safari MP4/AAC from the container.
        with av.open(io.BytesIO(data), mode="r") as container:
            streams = container.streams.audio
            if not streams:
                raise ValueError("no audio stream")
            resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=16000)
            for frame in container.decode(streams[0]):
                for converted in resampler.resample(frame):
                    samples = converted.to_ndarray().reshape(-1)
                    count += len(samples)
                    if count > max_seconds * 16000:
                        raise ValueError(f"audio longer than {max_seconds} seconds")
                    parts.append(samples.copy())
            for converted in resampler.resample(None):
                samples = converted.to_ndarray().reshape(-1)
                count += len(samples)
                if count > max_seconds * 16000:
                    raise ValueError(f"audio longer than {max_seconds} seconds")
                parts.append(samples.copy())
        if count < 1600:
            raise ValueError("audio shorter than 0.1 seconds")
        audio = np.concatenate(parts).astype("float32", copy=False)
        parts.clear()  # the per-frame copies are not needed beside the waveform during recognition
        decoded = time.perf_counter()
        with self.lock:
            acquired = time.perf_counter()
            # Read selection under the same lock as configure/download writes.
            settings = read_settings(STATE)
            path = str(model_directory(settings["model_path"]).resolve())
            model_loaded = self.model is None or self.loaded_path != path
            if model_loaded:
                self.model = None
                self.loaded_path = ""
                self.model = WhisperModel(path, device="cpu", compute_type="int8", local_files_only=True)
                self.loaded_path = path
            loaded = time.perf_counter()
            # faster-whisper returns a lazy generator: consume it before releasing
            # the lock so idle eviction cannot race a transcription.
            segments, _info = self.model.transcribe(audio, language=None, beam_size=3, vad_filter=True)
            words = " ".join(segment.text.strip() for segment in segments).strip()
            self.last_use = time.monotonic()
            inferred = time.perf_counter()
        if timings is not None:
            timings.update({
                "imports_ms": round((imported - started) * 1000, 3),
                "decode_ms": round((decoded - imported) * 1000, 3),
                "lock_wait_ms": round((acquired - decoded) * 1000, 3),
                "selection_and_load_ms": round((loaded - acquired) * 1000, 3),
                "inference_ms": round((inferred - loaded) * 1000, 3),
                "worker_total_ms": round((inferred - started) * 1000, 3),
                "model_loaded": model_loaded,
            })
        if len(words) > MAX_TRANSCRIPT_CHARS:
            raise ValueError("transcript too long")
        return words

    def install(self, name: str) -> None:
        if name not in MODEL_REPOS:
            raise ValueError("choose small or large-v3-turbo")
        self._control_lock()
        try:
            if self.installing:
                raise ValueError("a model download is already in progress")
            selected_at_start = read_settings(STATE)["model_path"]
            self.installing = name
            self.install_error = ""
            self.install_notice = ""
        finally:
            self.lock.release()

        def download() -> None:
            try:
                from huggingface_hub import snapshot_download
                # Explicit owner action only; never write into Program Files or
                # load HF code while serving a request. Cache is skill-owned.
                path = snapshot_download(
                    repo_id=MODEL_REPOS[name], cache_dir=str(STATE / "models"),
                    allow_patterns=["model.bin", "config.json", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"],
                )
                selected = str(model_directory(path))
                with self.lock:
                    settings = read_settings(STATE)
                    if settings["model_path"] == selected_at_start:
                        write_settings(STATE, validate_choice(selected, settings["always_resident"]))
                        self.model = None
                        self.loaded_path = ""
                    else:
                        # An owner choice made while the download was in flight
                        # must win over the older click that started it.
                        self.install_notice = "Model downloaded, but a newer selection was kept. Choose its snapshot in Settings if wanted."
            except Exception as exc:
                with self.lock:
                    self.install_error = f"{type(exc).__name__}: {exc}"
            finally:
                with self.lock:
                    self.installing = ""

        threading.Thread(target=download, daemon=True, name="whisper-model-download").start()


ENGINE = Engine()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = UPLOAD_IDLE_SECONDS  # an idle socket (a stalled upload) ends the request and frees the slot

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass  # Audio and the owner's transcript never enter companion logs.

    def _handle(self) -> None:
        if not secrets.compare_digest(self.headers.get("X-Voice-Token", ""), TOKEN):
            self._reply(403, error("forbidden", "Worker authorization failed"))
            return
        held = False
        try:
            if self.command == "GET" and self.path == "/status":
                self._reply(200, ENGINE.status())
                return
            if self.command != "POST" or self.path not in ("/transcribe", "/settings", "/models/install"):
                self._reply(404, error("not_found", "Unknown voice route"))
                return
            raw_length = self.headers.get("Content-Length", "")
            source = self.headers.get(SOURCE_HEADER, "dictation")
            if not raw_length.isdecimal() or int(raw_length) > limits_for(source if self.path == "/transcribe" else "dictation")[0]:
                self.close_connection = True
                self._reply(413, error("too_large", "Request too large"))
                return
            if self.path == "/transcribe":
                # The single recognition slot is reserved before the body is read: a second note is answered busy
                # with its body unread (the connection closes), and a sender that stalls frees the slot by the
                # idle timeout. The slot is released in the finally below.
                if not ENGINE.transcription.acquire(blocking=False):
                    self.close_connection = True
                    self._reply(503, error("voice_busy", "A local transcription is already running; try again after it finishes"))
                    return
                held = True
            data = self.rfile.read(int(raw_length))
            if len(data) != int(raw_length):
                raise ValueError("incomplete request body")
            if self.path == "/transcribe":
                timings = {}
                text = ENGINE.transcribe(data, self.headers.get("Content-Type", ""), timings, source, admitted=True)
                self._reply(200, {"text": text, "timings": timings,
                                  **({"status": "no_speech"} if not text else {})})
            else:
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError("expected an object")
                if self.path == "/settings":
                    if set(value) != {"model_path", "always_resident"}:
                        raise ValueError("expected model_path and always_resident")
                    settings = validate_choice(value["model_path"], value["always_resident"])
                    # Enforce local, readable snapshot before saving; a failed
                    # choice never replaces the last usable configuration.
                    self._reply(200, ENGINE.configure(settings))
                else:
                    ENGINE.install(value.get("model"))
                    self._reply(202, ENGINE.status())
        except VoiceBusy as exc:
            self._reply(503, error("voice_busy", str(exc)))
        except (ValueError, OSError, ImportError) as exc:
            self._reply(400, error("voice_unavailable", str(exc)))
        except Exception as exc:
            self._reply(502, error("inference_failed", f"{type(exc).__name__}: {exc}"))
        finally:
            if held:
                ENGINE.transcription.release()

    do_GET = _handle
    do_POST = _handle


def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    # A new worker rotates the token and invalidates the old endpoint even if
    # the old process lingers. Plugin reads the current private receipt each call.
    _atomic_json(ENDPOINT, {"port": server.server_port, "token": TOKEN, "pid": os.getpid()})
    threading.Thread(target=ENGINE.evict, daemon=True, name="whisper-idle-evict").start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            endpoint = json.loads(ENDPOINT.read_text(encoding="utf-8"))
            if endpoint.get("pid") == os.getpid() and endpoint.get("token") == TOKEN:
                ENDPOINT.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
