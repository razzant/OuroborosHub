"""Private bounded Telegram media capture, independent of Presence enrollment.

Provider callback metadata is persisted by DurableStore before this reader runs. A
published file is immutable for that message identity. Failure records a typed
status rather than passing a partial file or pretending an unavailable media
object was downloaded. Retention is deliberate owner work, never an age timer.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from telethon import errors as telegram_errors

INBOX_BYTES = 128 * 1024 * 1024
PHOTO_BYTES = 10 * 1024 * 1024
AUDIO_BYTES = 10 * 1024 * 1024
OPTIONAL_BYTES = 20 * 1024 * 1024
MIME = {"photo": "image/jpeg", "voice": "audio/ogg", "audio": "application/octet-stream",
        "video": "video/mp4", "document": "application/octet-stream",
        "unsupported": "application/octet-stream"}
SUFFIX = {"photo": ".jpg", "voice": ".ogg", "audio": ".ogg",
          "video": ".mp4", "document": ".bin", "unsupported": ".bin"}
_AUDIO_SUFFIX = {"audio/ogg": ".ogg", "audio/webm": ".webm",
                 "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
                 "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                 "audio/wav": ".wav", "audio/x-wav": ".wav",
                 "audio/flac": ".flac"}


def _suffix(kind: str | None, mime: str | None) -> str:
    if kind in {"audio", "voice"}:
        # The Host derives attachment MIME from the private file's suffix.
        # Unknown audio must stay opaque, not be relabelled as Ogg.
        return _AUDIO_SUFFIX.get((mime or "").split(";", 1)[0].lower(), ".bin")
    return SUFFIX.get(kind, ".bin")


class MediaLimit(RuntimeError):
    """The private media inbox cannot safely accept another download."""


class LegacyAudioReceipt(MediaLimit):
    """A pre-fix audio file must not be submitted under a false Ogg suffix."""


class MediaFetchUnavailable(RuntimeError):
    """A Telegram provider read failed; preserve raw input for retry."""


def describe(message: Any) -> dict[str, Any] | None:
    """Stable provider metadata only, never an expiring Telegram file reference."""
    media = getattr(message, "media", None)
    if media is None:
        return None
    if getattr(message, "photo", None) is not None:
        kind = "photo"
    elif getattr(message, "sticker", False) or getattr(message, "gif", False):
        kind = "unsupported"
    elif getattr(message, "voice", False):
        kind = "voice"
    elif getattr(message, "video", False) or getattr(message, "video_note", False):
        kind = "video"
    elif getattr(message, "audio", False):
        kind = "audio"
    elif getattr(message, "document", None) is not None:
        kind = "document"
    else:
        kind = "unsupported"
    size = getattr(getattr(message, "file", None), "size", None)
    if type(size) is not int or size < 0:
        size = None
    reported_mime = getattr(getattr(message, "file", None), "mime_type", None)
    essence = (reported_mime.split(";", 1)[0].strip().lower()
               if isinstance(reported_mime, str) and len(reported_mime) <= 80
               and reported_mime.isascii() else "")
    mime = (essence if "/" in essence and not any(c.isspace() for c in essence)
            else MIME[kind])
    return {"kind": kind, "size": size, "mime": mime}


def policy(config: Any, descriptor: dict) -> tuple[bool, int]:
    kind = descriptor["kind"]
    if kind == "photo":
        return True, PHOTO_BYTES
    if kind in {"audio", "voice"}:
        return True, AUDIO_BYTES
    if kind == "video":
        return bool(getattr(config, "download_video", False)), OPTIONAL_BYTES
    return bool(getattr(config, "download_documents", False)), OPTIONAL_BYTES


class MediaInbox:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir) / "media"
        if self.root.is_symlink():
            raise MediaLimit("media directory redirected")
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise MediaLimit("media directory redirected")

    def _paths(self, filename: str, kind: str | None = None,
               mime: str | None = None) -> tuple[Path, Path]:
        # The raw-ingress name is already a SHA-256 of provider identity.
        if len(filename) != 69 or not filename.endswith(".json") or not all(
                char in "0123456789abcdef" for char in filename[:64]):
            raise ValueError("invalid media source identity")
        stem = filename[:64]
        paths = (self.root / (stem + _suffix(kind, mime)), self.root / (stem + ".json"))
        if any(path.is_symlink() for path in paths):
            raise MediaLimit("media receipt redirected")
        return paths

    def _read_receipt(self, filename: str) -> dict | None:
        _file, receipt = self._paths(filename)
        if not receipt.exists():
            return None
        value = json.loads(receipt.read_bytes())
        if not isinstance(value, dict) or value.get("source") != filename:
            raise MediaLimit("media receipt invalid")
        return value

    def _publish_receipt(self, filename: str, value: dict) -> dict:
        _file, target = self._paths(filename)
        if target.exists():
            old = self._read_receipt(filename)
            if old != value:
                raise MediaLimit("media identity changed")
            return old
        temp = self.root / ("." + secrets.token_hex(16) + ".tmp")
        try:
            with temp.open("xb") as stream:
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, target)  # never overwrite a first receipt on callback replay
        except FileExistsError:
            old = self._read_receipt(filename)
            if old != value:
                raise MediaLimit("media identity changed")
            return old
        finally:
            temp.unlink(missing_ok=True)
        return value

    def _used(self) -> int:
        total = 0
        for path in self.root.iterdir():
            if path.is_symlink():
                raise MediaLimit("media directory contains redirected entry")
            if path.suffix in set(SUFFIX.values()) | set(_AUDIO_SUFFIX.values()) | {".tmp"}:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise MediaLimit("media directory contains non-file entry")
                total += info.st_size
        return total

    async def ensure(self, filename: str, descriptor: dict, config: Any, adapter: Any,
                     peer: int, message_id: int, *, observed: Any = None) -> dict:
        existing = self._read_receipt(filename)
        if existing is not None:
            return existing
        file, _receipt = self._paths(filename, descriptor["kind"], descriptor["mime"])
        allowed, limit = policy(config, descriptor)
        base = {"source": filename, "kind": descriptor["kind"], "mime": descriptor["mime"],
                "suffix": _suffix(descriptor["kind"], descriptor["mime"])}
        if descriptor["kind"] == "unsupported":
            return self._publish_receipt(filename, {**base, "status": "unsupported_media"})
        if not allowed:
            return self._publish_receipt(filename, {**base, "status": "download_disabled"})
        size = descriptor.get("size")
        if type(size) is int and size > limit:
            return self._publish_receipt(filename, {**base, "status": "media_too_large"})
        if file.exists():
            # Crash between publication and receipt. Do not overwrite bytes or
            # invent identity from an unmeasured preimage; hold for repair.
            raise MediaLimit("unreceipted media file")
        if self._used() + (size if type(size) is int else limit) > INBOX_BYTES:
            raise MediaLimit("media inbox byte budget reached")
        media = observed
        if media is None:
            try:
                message = await adapter.client.get_messages(peer, ids=message_id)
            except (telegram_errors.RPCError, OSError, TimeoutError) as exc:
                raise MediaFetchUnavailable("Telegram media lookup unavailable") from exc
            if message is None or getattr(message, "id", None) != message_id or describe(message) != descriptor:
                return self._publish_receipt(filename, {**base, "status": "provider_media_unavailable"})
            media = getattr(message, "media", None)
        temp = self.root / ("." + secrets.token_hex(16) + ".tmp")
        digest = hashlib.sha256()
        count = 0
        try:
            with temp.open("xb") as stream:
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                chunks = adapter.client.iter_download(media, request_size=65536)
                while True:
                    try:
                        chunk = await anext(chunks)
                    except StopAsyncIteration:
                        break
                    except (telegram_errors.RPCError, OSError, TimeoutError) as exc:
                        raise MediaFetchUnavailable("Telegram media stream unavailable") from exc
                    if not isinstance(chunk, bytes) or count + len(chunk) > limit:
                        raise MediaLimit("media download exceeded per-kind limit")
                    count += len(chunk)
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not count or (size is not None and count != size):
                raise MediaLimit("media download size mismatch")
            os.link(temp, file)
        except MediaLimit as exc:
            if str(exc) in {"media download exceeded per-kind limit", "media download size mismatch"}:
                return self._publish_receipt(filename, {**base, "status": "download_limit_exceeded"})
            raise
        finally:
            temp.unlink(missing_ok=True)
        return self._publish_receipt(filename, {**base, "status": "ready", "bytes": count,
                                                "sha256": digest.hexdigest()})

    def ready_file(self, filename: str) -> tuple[Path, dict] | None:
        receipt = self._read_receipt(filename)
        if receipt is None or receipt.get("status") != "ready":
            return None
        if ("suffix" not in receipt and receipt["kind"] in {"audio", "voice"}
                and receipt.get("mime") != "audio/ogg"):
            raise LegacyAudioReceipt("legacy audio suffix ambiguous; retain bytes for explicit repair")
        if "suffix" in receipt and receipt["suffix"] != _suffix(receipt["kind"], receipt.get("mime")):
            raise MediaLimit("media receipt suffix changed")
        file, _ = self._paths(filename, receipt["kind"], receipt.get("mime"))
        if file.is_symlink() or not file.is_file() or file.stat().st_size != receipt.get("bytes"):
            raise MediaLimit("media file unavailable")
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != receipt.get("sha256"):
            raise MediaLimit("media file changed")
        if ("suffix" not in receipt and receipt["kind"] in {"audio", "voice"}
                and receipt.get("mime") == "audio/ogg"):
            # The old generic-audio fallback labelled unknown bytes Ogg. A
            # matching digest proves only preservation, not the container.
            with file.open("rb") as stream:
                if stream.read(4) != b"OggS":
                    raise LegacyAudioReceipt("legacy Ogg audio signature unproven; retain bytes for repair")
        return file, receipt
