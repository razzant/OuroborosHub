"""Small, auditable skill-local protocol and read-only model selection.

No code in this module loads the 1.6 GB model. The supervised companion alone owns it.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

# One constant per limit; the manifest row, tool text, widget text, route and companion all read these.
# Dictation (the composer's POST route) is bounded by core's ceilings for a `provides: stt` row:
# ouroboros/extension_ui_validation.py clamps max_bytes to 524288 and max_seconds to 600, the gateway's
# child-dispatch body cap is 512 KiB (gateway/extensions.py), and the composer recorder clamps to both
# (web/modules/voice_recorder.js, speech_providers.js). SKILL.md's contributes row repeats these two numbers.
DICTATION_MAX_BYTES = 524288
DICTATION_MAX_SECONDS = 600
# An attached recording (transcribe_attachment, e.g. a Telegram voice note) never crosses that ingress: the tool
# reads the host-staged file and posts it to the loopback companion; core's staging puts no size cap of its own
# on it, so the ceiling is what a connector can hand over: 20 MiB is the Bot API download limit of the Hub's
# telegram-bot (CLOUD_DOWNLOAD_LIMIT), while core's built-in skills/telegram stops at 10 MiB
# (_MAX_TELEGRAM_DOWNLOAD_BYTES). The length cap is the skill's own working bound for memory (decoded
# 16 kHz float32, 115 MB at the cap) and for the tool deadline below.
ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
ATTACHMENT_MAX_SECONDS = 1800
SOURCE_HEADER = "X-Voice-Source"
LIMITS = {"dictation": (DICTATION_MAX_BYTES, DICTATION_MAX_SECONDS),
          "attachment": (ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_SECONDS)}
# Transcript text a 30-minute recording can hold (about 15 characters a second), and the worker reply read
# bound that fits it in UTF-8 (2 bytes per Cyrillic character) plus JSON framing.
MAX_TRANSCRIPT_CHARS = 60000
MAX_REPLY_BYTES = 262144
# Recognition runs on CPU; the HTTP wait on the worker and the tool deadline outlast the longest accepted recording.
WORKER_TIMEOUT_SECONDS = 1500
TOOL_TIMEOUT_SECONDS = 1560
IDLE_SECONDS = 3600
DEFAULT_RECEIPT = r"C:\Program Files\Helene\data\models\whisper\installed.json"
REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")
_SETTINGS_WRITE_LOCK = threading.Lock()


class ModelUnavailable(ValueError):
    pass


def settings_path(state_dir: Path) -> Path:
    return state_dir / "voice_settings.json"


def read_settings(state_dir: Path) -> dict:
    path = settings_path(state_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("settings must be an object")
    except FileNotFoundError:
        value = {}
    except (OSError, UnicodeError, ValueError) as exc:
        raise ModelUnavailable(f"voice settings unreadable: {exc}") from exc
    try:
        # Hélène's receipt is only a convenience on hosts that actually have
        # it. Else the skill's own models directory remains configurable even
        # before the first download; no Windows path leaks into a Unix choice.
        default = (DEFAULT_RECEIPT if Path(DEFAULT_RECEIPT).is_file()
                   else str((Path(state_dir) / "models").resolve()))
        return validate_choice(value.get("model_path", default), value.get("always_resident", False))
    except ValueError as exc:
        raise ModelUnavailable(f"voice settings invalid: {exc}") from exc


def validate_choice(path: object, always_resident: object) -> dict:
    if not isinstance(path, str) or not path.strip() or len(path) > 4096:
        raise ValueError("model_path must be a nonempty local filesystem path")
    path = path.strip()
    # An owner-selected local pathname is not a model repository id or URL.
    if "://" in path or (urlsplit(path).scheme not in ("",)
                        and not (os.name == "nt" and len(path) > 2 and path[1] == ":")):
        raise ValueError("model_path must be a local filesystem path, not a URL")
    if not Path(path).is_absolute():
        raise ValueError("model_path must be absolute")
    if not isinstance(always_resident, bool):
        raise ValueError("always_resident must be a boolean")
    return {"model_path": path, "always_resident": always_resident}


def write_settings(state_dir: Path, value: dict) -> None:
    # Windows may refuse concurrent replace of the same target, even with
    # distinct temp files. Serialize this companion's Settings/download writes.
    with _SETTINGS_WRITE_LOCK:
        state_dir.mkdir(parents=True, exist_ok=True)
        target = settings_path(state_dir)
        tmp = state_dir / f"voice_settings.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            with tmp.open("x", encoding="utf-8") as stream:
                os.chmod(tmp, 0o600)
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)


def model_directory(path: str) -> Path:
    choice = validate_choice(path, False)["model_path"]
    selected = Path(choice)
    if selected.name.lower() == "installed.json":
        try:
            receipt = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ModelUnavailable(f"model receipt unreadable: {exc}") from exc
        if (not isinstance(receipt, dict) or receipt.get("schema") != "helene.voice.v1"
                or receipt.get("model") != "turbo"):
            raise ModelUnavailable("model receipt does not describe an installed Hélène turbo snapshot")
        path_value = receipt.get("path")
        if not isinstance(path_value, str):
            raise ModelUnavailable("model receipt has no snapshot path")
        choice = validate_choice(path_value, False)["model_path"]
        selected = Path(choice)
    if not selected.is_dir():
        raise ModelUnavailable("selected local model directory is missing")
    for name in REQUIRED_MODEL_FILES:
        file = selected / name
        if not file.is_file() or file.stat().st_size == 0:
            raise ModelUnavailable(f"model snapshot lacks {name}")
    return selected


def limits_for(source: str) -> tuple[int, int]:
    """(max_bytes, max_seconds) of a request source; anything unnamed is dictation, the stricter one."""
    return LIMITS.get(source, LIMITS["dictation"])


def audio_format(mime: str, data: bytes, max_bytes: int = DICTATION_MAX_BYTES) -> None:
    mime = mime.split(";", 1)[0].strip().lower()
    if mime not in ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg"):
        raise ValueError("unsupported audio type; use WebM, Ogg, MP4 or MP3")
    if not data or len(data) > max_bytes:
        raise ValueError(f"audio must contain 1..{max_bytes} bytes")
    valid = (data.startswith(b"\x1a\x45\xdf\xa3") if mime == "audio/webm" else
             data.startswith(b"OggS") if mime == "audio/ogg" else
             data[4:8] == b"ftyp" if mime == "audio/mp4" else
             data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0))
    if not valid:
        raise ValueError("audio container signature does not match Content-Type")


def error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
