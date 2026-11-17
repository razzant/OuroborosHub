"""The skill's own copies of files queued for sending (0.12.1).

``telegram_send`` copies each file here when it queues it, unchanged and under its own name, and records the copy's
size and SHA-256 and the path it came from in the row as ``_frozen``. The outbox sends that copy, checked against the
record before each attempt. The copy stays with the row's custody history, as the row does, whatever the row's end:
an uncertain send keeps the exact bytes it may have delivered. The caller's file is read once and never changed,
moved or removed. Only a copy that no row names, made by a ``telegram_send`` that queued nothing, is removed.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import uuid
from typing import Any, Dict, List

DIRECTORY = "outbox_files"
_CHUNK = 1024 * 1024


class CopyTooLarge(ValueError):
    """The file grew past the upload limit while it was being copied; nothing was kept."""


def keep(source: pathlib.Path, state_dir: Any, max_bytes: int) -> Dict[str, Any]:
    """Copy ``source`` into a new folder of its own and return the row's record of the copy.

    The copy reaches the disk (fsync) before the row that names it can be queued, so a row never names a partial
    copy. Any failure removes what was written and raises: ``OSError`` or ``CopyTooLarge``.
    """
    source = pathlib.Path(source)
    root = pathlib.Path(state_dir) / DIRECTORY
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = root / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    target = folder / source.name
    digest, size = hashlib.sha256(), 0
    try:
        with open(source, "rb") as reader:
            # Readable by this account only, whatever the original allowed.
            created = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(created, "wb") as writer:
                while chunk := reader.read(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise CopyTooLarge(f"{source.name} grew past {max_bytes:,} bytes while it was copied")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
    except BaseException:
        discard([{"path": str(target)}], state_dir)
        raise
    return {"path": str(target), "sha256": digest.hexdigest(), "size": size, "source": str(source)}


def records(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The copies a row names. A row queued before 0.12.1 names none: its file is the path it named, unproven."""
    kept = payload.get("_frozen")
    return [item for item in kept if isinstance(item, dict) and item.get("path")] if isinstance(kept, list) else []


def verify(payload: Dict[str, Any]) -> None:
    """Raise ``ValueError`` unless every copy the row names still has the size and SHA-256 recorded when queued."""
    for record in records(payload):
        name = pathlib.PurePath(str(record["path"])).name
        digest, size = hashlib.sha256(), 0
        try:
            with open(record["path"], "rb") as reader:
                while chunk := reader.read(_CHUNK):
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise ValueError(f"the copy of {name} kept when it was queued cannot be read ({type(exc).__name__}); "
                             "not sent") from exc
        if size != record.get("size") or digest.hexdigest() != record.get("sha256"):
            raise ValueError(f"the copy of {name} kept when it was queued has changed since; not sent")


def discard(kept: List[Dict[str, Any]], state_dir: Any) -> None:
    """Remove copies that no row names: a ``telegram_send`` that queued nothing made them.

    Only a file directly inside one of this skill's copy folders is removed, never anything outside them.
    """
    root = os.path.realpath(pathlib.Path(state_dir) / DIRECTORY)
    for record in kept:
        path = str(record.get("path") or "")
        folder = os.path.realpath(os.path.dirname(path))
        if not path or os.path.dirname(folder) != root:
            continue
        try:
            os.unlink(os.path.join(folder, os.path.basename(path)))
        except FileNotFoundError:
            pass
        try:
            os.rmdir(folder)
        except OSError:
            pass
