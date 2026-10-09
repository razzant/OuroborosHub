"""Activation lease: an OS file lock the server process holds while the skill is loaded.

The server-side supervised task takes an exclusive lock on ``activation.lock`` for
its whole lifetime (one holder at a time). It then picks a fresh activation epoch,
takes a second exclusive lock on that epoch's own file (``epoch_lock_path``), and
only then records the epoch in the state. Any process (a task worker sending mail,
the server's own routes) asks ``epoch_alive`` for the epoch the state names: a live
holder of exactly that epoch is proven only when a non-blocking shared probe of its
file meets ordinary lock contention. A new holder never locks an earlier epoch's
file, so in the gap before it records its own epoch it vouches for nothing. The OS
releases the locks when the holder process dies, so no TTL or heartbeat pretends a
process is alive.

Every other failure — no file, locking unsupported or unavailable (``ENOLCK``,
``EOPNOTSUPP``), a bad descriptor, anything unexpected — proves nothing and is
reported as "no live holder", so the contact stage fails closed.

POSIX uses ``flock`` (per open file description, so a probe from the holder's own
process through a second descriptor still conflicts; measured on macOS 26 for the
0.1.1 repair). Windows uses ``LockFileEx`` on
byte 0: the holder takes it exclusive, a probe asks for it SHARED and non-blocking
(``LOCKFILE_FAIL_IMMEDIATELY``), so two probes never contend with each other and
only ``ERROR_LOCK_VIOLATION`` against the holder's exclusive lock proves a holder.
Windows byte-range locks belong to the handle, so a probe through another handle of
the holder's own process conflicts too. Anything else is reported as unsupported and
the contact stage refuses to arm.
"""

from __future__ import annotations

import errno
import os
import pathlib
from typing import Optional

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

# flock: EWOULDBLOCK (equal to EAGAIN on most systems) when ANOTHER holder has the lock.
_POSIX_CONTENTION = frozenset({errno.EWOULDBLOCK, errno.EAGAIN})
_ERROR_LOCK_VIOLATION = 33   # Windows: the range is locked by another handle


class LockBusy(OSError):
    """Windows only: ``LockFileEx`` met another handle's conflicting lock (ERROR_LOCK_VIOLATION)."""


class _Win32Locks:  # pragma: no cover - exercised natively only on Windows
    """``LockFileEx``/``UnlockFileEx`` on byte 0 of an open descriptor, never blocking."""

    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2

    def __init__(self) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for name in ("LockFileEx", "UnlockFileEx"):
            fn = getattr(kernel32, name)
            fn.restype = wintypes.BOOL
        kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(_Overlapped)]
        kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.POINTER(_Overlapped)]
        self._ctypes, self._msvcrt, self._kernel32, self._overlapped = ctypes, msvcrt, kernel32, _Overlapped

    def lock(self, fd: int, *, exclusive: bool) -> None:
        flags = self._LOCKFILE_FAIL_IMMEDIATELY | (self._LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
        handle = self._msvcrt.get_osfhandle(fd)
        if not self._kernel32.LockFileEx(handle, flags, 0, 1, 0, self._ctypes.byref(self._overlapped())):
            self._raise(self._ctypes.get_last_error(), "LockFileEx")

    def unlock(self, fd: int) -> None:
        handle = self._msvcrt.get_osfhandle(fd)
        if not self._kernel32.UnlockFileEx(handle, 0, 1, 0, self._ctypes.byref(self._overlapped())):
            self._raise(self._ctypes.get_last_error(), "UnlockFileEx")

    @staticmethod
    def _raise(winerror: int, call: str) -> None:
        if winerror == _ERROR_LOCK_VIOLATION:
            raise LockBusy(errno.EACCES, f"{call}: lock violation")
        raise OSError(errno.EIO, f"{call} failed (winerror {winerror})")


_win: Optional[object] = None
if _fcntl is None:  # pragma: no cover - Windows
    try:
        _win = _Win32Locks()
    except (ImportError, OSError, AttributeError):
        _win = None


def supported() -> bool:
    return _fcntl is not None or _win is not None


def _lock(fd: int, *, exclusive: bool) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, (_fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH) | _fcntl.LOCK_NB)
    else:
        _win.lock(fd, exclusive=exclusive)


def _unlock(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    else:
        _win.unlock(fd)


def _contention(exc: OSError) -> bool:
    """Only ordinary lock contention proves that some live process holds the lock."""
    if _fcntl is not None:
        return exc.errno in _POSIX_CONTENTION
    return isinstance(exc, LockBusy)


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, "unknown")


class Lease:
    """An exclusive, process-lifetime lock on one file. Release is idempotent."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self._fd: Optional[int] = None
        self.last_error = ""

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the exclusive lock without blocking; False if it is held elsewhere or cannot be taken."""
        if self._fd is not None:
            return True
        if not supported():
            self.last_error = "unsupported"
            return False
        try:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            self.last_error = _errno_name(exc)
            return False
        try:
            _lock(fd, exclusive=True)
        except OSError as exc:
            os.close(fd)
            self.last_error = "held" if _contention(exc) else _errno_name(exc)
            return False
        self._fd = fd
        self.last_error = ""
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


_EPOCH_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
_EPOCH_PREFIX = "activation-"


def epoch_lock_path(directory: pathlib.Path, epoch: object) -> Optional[pathlib.Path]:
    """The lock file of one activation epoch; None for anything but a short plain name."""
    text = str(epoch or "")
    if not text or len(text) > 64 or not set(text) <= _EPOCH_CHARS:
        return None
    return pathlib.Path(directory) / f"{_EPOCH_PREFIX}{text}.lock"


def epoch_alive(directory: pathlib.Path, epoch: object) -> bool:
    """True only when lock contention proves a live holder of exactly this epoch."""
    path = epoch_lock_path(directory, epoch)
    return path is not None and holder_alive(path)


def remove_epoch_locks(directory: pathlib.Path) -> None:
    """Best effort: delete earlier epochs' lock files. Call only while holding ``activation.lock``:
    a holder releases its epoch lock before that one, so no live process holds any of these. A
    probe of a deleted file proves nothing, like a released one."""
    for path in pathlib.Path(directory).glob(f"{_EPOCH_PREFIX}*.lock"):
        try:
            path.unlink()
        except OSError:      # for example still open in a probe on Windows; the next holder retries
            pass


def holder_alive(path: pathlib.Path) -> bool:
    """True only when lock contention proves that some live process holds the activation lock.

    The probe never creates the file: no file means no activation ever happened.
    """
    if not supported():
        return False
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            _lock(fd, exclusive=False)
        except OSError as exc:
            return _contention(exc)
        except Exception:
            return False
        try:
            _unlock(fd)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)
