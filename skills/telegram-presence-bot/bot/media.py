"""Private bounded Bot API media spool before any Presence admission."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

from .transport import MediaTooLarge

MAX_TOTAL_BYTES = 128 * 1024 * 1024
LIMITS = {"photo": 10 * 1024 * 1024, "voice": 10 * 1024 * 1024,
          "audio": 10 * 1024 * 1024, "video": 20 * 1024 * 1024,
          "document": 20 * 1024 * 1024}
SUFFIX = {"photo": ".jpg", "voice": ".ogg", "audio": ".ogg",
          "video": ".mp4", "document": ".bin", "unsupported": ".bin"}
_AUDIO_SUFFIX = {"audio/ogg": ".ogg", "audio/webm": ".webm",
                 "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
                 "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                 "audio/wav": ".wav", "audio/x-wav": ".wav",
                 "audio/flac": ".flac"}


class LegacyAudioReceipt(ValueError):
    """A pre-fix audio file must not be submitted under a false Ogg suffix."""


def _suffix(kind: str | None, mime: str | None) -> str:
    if kind in {"audio", "voice"}:
        return _AUDIO_SUFFIX.get((mime or "").split(";", 1)[0].lower(), ".bin")
    return SUFFIX.get(kind, ".bin")


def describe(message: dict) -> dict | None:
    if type(message) is not dict:
        return None
    if type(message.get("photo")) is list and message["photo"]:
        kind, item = "photo", message["photo"][-1]
    else:
        kind, item = next(((k, message[k]) for k in
                           ("voice", "audio", "video", "video_note", "document",
                            "sticker", "animation")
                           if type(message.get(k)) is dict), (None, None))
        if kind == "video_note":
            kind = "video"
        if kind in {"sticker", "animation"}:
            kind = "unsupported"
    if not kind or type(item) is not dict:
        return None
    fid = item.get("file_id")
    if type(fid) is not str or not 1 <= len(fid) <= 512:
        return None
    size = item.get("file_size")
    if type(size) is not int or size < 0:
        size = None
    default = {"photo": "image/jpeg", "voice": "audio/ogg", "audio": "application/octet-stream",
               "video": "video/mp4", "document": "application/octet-stream",
               "unsupported": "application/octet-stream"}[kind]
    reported_mime = item.get("mime_type", default)
    essence = (reported_mime.split(";", 1)[0].strip().lower()
               if type(reported_mime) is str and len(reported_mime) <= 80
               and reported_mime.isascii() else "")
    mime = essence if "/" in essence and not any(c.isspace() for c in essence) else default
    return {"kind": kind, "file_id": fid, "size": size, "mime": mime}


class MediaStore:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir) / "media"
        if self.root.is_symlink():
            raise ValueError("private media directory redirected")
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("private media directory redirected")

    def _paths(self, update_id: int, kind: str | None = None,
               mime: str | None = None) -> tuple[Path, Path]:
        if type(update_id) is not int or update_id < 0:
            raise ValueError("invalid media update identity")
        name = str(update_id)
        paths = self.root / (name + _suffix(kind, mime)), self.root / (name + ".json")
        if any(path.is_symlink() for path in paths):
            raise ValueError("private media path redirected")
        return paths

    def _receipt(self, update_id: int) -> dict | None:
        _, path = self._paths(update_id)
        if not path.exists():
            return None
        data = json.loads(path.read_bytes())
        if type(data) is not dict or data.get("update_id") != update_id:
            raise ValueError("media receipt invalid")
        return data

    def _publish(self, update_id: int, receipt: dict) -> dict:
        _, target = self._paths(update_id)
        previous = self._receipt(update_id)
        if previous is not None:
            if previous != receipt:
                raise ValueError("media source identity changed")
            return previous
        temp = self.root / ("." + secrets.token_hex(16) + ".tmp")
        try:
            with temp.open("xb") as stream:
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                old = self._receipt(update_id)
                if old != receipt:
                    raise ValueError("media source identity changed")
        finally:
            temp.unlink(missing_ok=True)
        return receipt

    def _used(self) -> int:
        total = 0
        for file in self.root.iterdir():
            if file.is_symlink():
                raise ValueError("media directory redirected")
            if file.suffix in set(SUFFIX.values()) | set(_AUDIO_SUFFIX.values()) | {".tmp"}:
                info = file.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("media file is not regular")
                total += info.st_size
        return total

    def capture(self, update_id: int, descriptor: dict, config: dict, transport) -> dict:
        existing = self._receipt(update_id)
        if existing is not None:
            if existing.get("file_id") != descriptor["file_id"]:
                raise ValueError("provider file identity changed")
            return existing
        file, _ = self._paths(update_id, descriptor["kind"], descriptor["mime"])
        if file.exists():
            raise ValueError("unreceipted private media file")
        kind = descriptor["kind"]
        base = {"update_id": update_id, "kind": kind, "file_id": descriptor["file_id"],
                "mime": descriptor["mime"], "suffix": _suffix(kind, descriptor["mime"])}
        if kind == "unsupported":
            return self._publish(update_id, {**base, "status": "unsupported_media"})
        if (kind == "video" and not config["download_video"] or
                kind == "document" and not config["download_documents"]):
            return self._publish(update_id, {**base, "status": "download_disabled"})
        limit = LIMITS[kind]
        if descriptor["size"] is not None and descriptor["size"] > limit:
            return self._publish(update_id, {**base, "status": "media_too_large"})
        if self._used() + (descriptor["size"] if type(descriptor["size"]) is int else limit) > MAX_TOTAL_BYTES:
            raise ValueError("private media byte budget reached")
        temp = self.root / ("." + secrets.token_hex(16) + ".tmp")
        try:
            # A failed GET/getFile is not a permanent provider refusal. The
            # original raw update remains pending and can retry this READ with
            # its frozen file_id; never publish a false immutable no-bytes row.
            try:
                size, digest = transport.download(descriptor["file_id"], temp, byte_limit=limit)
            except MediaTooLarge:
                return self._publish(update_id, {**base, "status": "media_too_large"})
            if descriptor["size"] is not None and size != descriptor["size"]:
                return self._publish(update_id, {**base, "status": "size_mismatch"})
            if file.exists():
                raise ValueError("media source identity changed")
            os.link(temp, file)
        finally:
            temp.unlink(missing_ok=True)
        return self._publish(update_id, {**base, "status": "ready", "bytes": size, "sha256": digest})

    def ready_file(self, update_id: int) -> tuple[Path, dict] | None:
        receipt = self._receipt(update_id)
        if receipt is None or receipt.get("status") != "ready":
            return None
        if ("suffix" not in receipt and receipt["kind"] in {"audio", "voice"}
                and receipt.get("mime") != "audio/ogg"):
            raise LegacyAudioReceipt("legacy audio suffix ambiguous; retain bytes for explicit repair")
        if "suffix" in receipt and receipt["suffix"] != _suffix(receipt["kind"], receipt.get("mime")):
            raise ValueError("media receipt suffix changed")
        file, _ = self._paths(update_id, receipt["kind"], receipt.get("mime"))
        if file.is_symlink() or not file.is_file() or file.stat().st_size != receipt.get("bytes"):
            raise ValueError("private media file unavailable")
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != receipt.get("sha256"):
            raise ValueError("private media changed")
        if ("suffix" not in receipt and receipt["kind"] in {"audio", "voice"}
                and receipt.get("mime") == "audio/ogg"):
            with file.open("rb") as stream:
                if stream.read(4) != b"OggS":
                    raise LegacyAudioReceipt("legacy Ogg audio signature unproven; retain bytes for repair")
        return file, receipt
