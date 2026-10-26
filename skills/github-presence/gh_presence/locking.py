"""One companion per local state directory, held by the OS until close/exit."""
from __future__ import annotations

import errno
import os


class CompanionAlreadyRunning(RuntimeError):
    pass


def _windows_lock(fd, unlock=False):
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    # Windows permits locking beyond EOF. Do not write/truncate the shared
    # file before ownership, and always lock/unlock the same byte.
    msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)


def _posix_lock(fd, unlock=False):
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


class CompanionLock:
    def __init__(self, path):
        self.path, self.fd = path, None
        self._lock = _windows_lock if os.name == "nt" else _posix_lock

    def __enter__(self):
        if self.fd is not None:
            raise RuntimeError("companion_lock_already_acquired")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.set_inheritable(fd, False)
            self._lock(fd)
        except BaseException as exc:
            os.close(fd)
            if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise CompanionAlreadyRunning("another github-presence companion holds this state") from None
            raise
        self.fd = fd
        return self

    def __exit__(self, *args):
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                self._lock(fd, unlock=True)
            finally:
                os.close(fd)
        # Keep the inode/path in place: unlinking permits two different locks
        # to protect the same state. A stale file alone conveys no ownership.
