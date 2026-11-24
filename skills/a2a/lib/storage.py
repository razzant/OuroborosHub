"""Durable small-file helpers for the link store and the letter journal (standard library only)."""
from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator


def read_json(path: Path) -> Any:
    """The parsed document, or None when the file does not exist; an unreadable file raises ValueError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{Path(path).name} is unreadable") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{Path(path).name} is not valid JSON") from exc


def write_bytes(path: Path, data: bytes, *, private: bool = False, exclusive: bool = False) -> None:
    """Replace ``path`` atomically (``exclusive``: create it only if absent, else FileExistsError).

    A ``private`` file is created owner-only (0600) before any byte is written, so a secret never
    sits in a world-readable file even for a moment.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if private else 0o644
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def write_json(path: Path, value: Any, *, private: bool = False) -> None:
    write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"), private=private)


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the sidecar file ``path`` across threads and processes.

    Tool calls run in separate child processes, so a re-read and its conditional write must be one
    step under this lock, or a stale writer could erase what another call just recorded.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt":  # pragma: no cover - exercised on Windows installs
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
