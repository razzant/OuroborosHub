"""A synthetic stand-in for the three names Context Lens borrows from core's ``ouroboros.store``.

This is NOT core, and a pass against it does not verify core's helpers. It
mirrors their documented contract so that the reader's USE of them can be
tested without importing or executing an Ouroboros:

* ``counted_connect(target, path, **kwargs)`` opens ``sqlite3.connect(target,
  factory=..., **kwargs)`` and counts the connection on ``path``'s inode until
  ``close()``, as core's ``store._StoreConnection`` does;
* ``_journal_mode(path, tier)`` answers ``"WAL"`` or ``"PERSIST"`` as configured
  (core decides from the SQLite version, the lock tier and the filesystem);
* ``wal_file(path)`` reads header bytes 18-19. Core reads them through its inode
  registry; this stand-in reads with ``os.open``/``os.close``, which is safe
  only in a test process that holds no SQLite lock on the file at that moment.

Leaving a name out models a core that lacks it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import types
from typing import Any, Dict, List, Optional, Tuple


# Raw reads open descriptors in binary mode: on Windows os.open would otherwise
# translate line endings and stop at Ctrl-Z.
READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)


def read_at(fd: int, size: int, offset: int) -> bytes:
    """``size`` bytes at ``offset`` of ``fd`` (fewer only at end of file).

    ``os.pread`` where the platform has it (POSIX), otherwise seek and read,
    which on Windows moves only this descriptor's own position.
    """
    chunks: List[bytes] = []
    done = 0
    while done < size:
        if hasattr(os, "pread"):
            chunk = os.pread(fd, size - done, offset + done)
        else:
            os.lseek(fd, offset + done, os.SEEK_SET)
            chunk = os.read(fd, size - done)
        if not chunk:
            break
        chunks.append(chunk)
        done += len(chunk)
    return b"".join(chunks)


class Registry:
    """What the stand-in observed: counted connections per inode and every call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.open: Dict[Tuple[int, int], int] = {}
        self.opened = 0
        self.closed = 0
        self.peak = 0
        self.calls: List[Tuple[str, Any]] = []

    def record(self, name: str, details: Any) -> None:
        with self._lock:
            self.calls.append((name, details))

    def count(self, key: Tuple[int, int]) -> None:
        with self._lock:
            self.open[key] = self.open.get(key, 0) + 1
            self.opened += 1
            self.peak = max(self.peak, sum(self.open.values()))

    def uncount(self, key: Tuple[int, int]) -> None:
        with self._lock:
            self.open[key] -= 1
            if not self.open[key]:
                del self.open[key]
            self.closed += 1

    def total_open(self) -> int:
        with self._lock:
            return sum(self.open.values())

    def names(self) -> List[str]:
        with self._lock:
            return [name for name, _ in self.calls]


class _Counted(sqlite3.Connection):
    _registry: Optional[Registry] = None
    _key: Optional[Tuple[int, int]] = None

    def close(self) -> None:
        super().close()
        registry, key = self._registry, self._key
        self._registry = self._key = None
        if registry is not None and key is not None:
            registry.uncount(key)


def make_core(*, eligible: bool = True, policy: bool = True, counted: bool = True,
              header: bool = True, policy_error: Optional[BaseException] = None) -> types.ModuleType:
    """A module-shaped stand-in; ``module.registry`` is what it observed."""
    registry = Registry()
    module = types.ModuleType("core_store_standin")
    module.TIER_ENFORCED = "enforced"
    module.registry = registry

    if counted:
        def counted_connect(target: str, path: Any, **kwargs: Any) -> sqlite3.Connection:
            registry.record("counted_connect", (target, str(path), dict(kwargs)))
            conn = sqlite3.connect(target, factory=_Counted, **kwargs)
            try:
                info = os.stat(path)
            except OSError:
                conn.close()
                raise
            key = (info.st_dev, info.st_ino)
            registry.count(key)
            conn._registry, conn._key = registry, key
            return conn
        module.counted_connect = counted_connect

    if policy:
        def _journal_mode(path: Any, tier: str) -> str:
            registry.record("_journal_mode", (path, tier))
            if policy_error is not None:
                raise policy_error
            return "WAL" if eligible and tier == "enforced" else "PERSIST"
        module._journal_mode = _journal_mode

    if header:
        def wal_file(path: Any) -> bool:
            registry.record("wal_file", str(path))
            try:
                fd = os.open(os.fspath(path), READ_FLAGS)
            except FileNotFoundError:
                return False
            try:
                head = read_at(fd, 20, 0)
            finally:
                os.close(fd)
            return len(head) >= 20 and head[18] == 2 and head[19] == 2
        module.wal_file = wal_file

    return module
