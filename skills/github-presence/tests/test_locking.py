"""Native competing processes plus the Windows CRT byte-lock call contract."""
from __future__ import annotations

import concurrent.futures
import errno
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gh_presence.locking import CompanionAlreadyRunning, CompanionLock, _windows_lock
from gh_presence.store import Store


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="github-companion-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "companion.lock"
        self.env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                    "PYTHONDONTWRITEBYTECODE": "1", "OUROBOROS_SKILL_STATE_DIR": self.tmp.name}
        if os.name == "nt":
            self.env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

    def process(self, code):
        proc = subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT), str(self.path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=self.env, cwd=self.tmp.name)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)
        self.addCleanup(cleanup)
        return proc

    def attempt(self):
        proc = self.process("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                            "from gh_presence.locking import CompanionLock; "
                            "lock=CompanionLock(sys.argv[2]); lock.__enter__(); print('acquired'); lock.__exit__()")
        out, err = proc.communicate(timeout=10)
        return proc.returncode, out, err

    def holder(self):
        proc = self.process("import sys; sys.path.insert(0,sys.argv[1]); from gh_presence.locking import CompanionLock; "
                            "lock=CompanionLock(sys.argv[2]); lock.__enter__(); print('ready',flush=True); "
                            "sys.stdin.read(1); lock.__exit__()")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            ready = pool.submit(proc.stdout.readline)
            try:
                self.assertEqual(ready.result(timeout=10).strip(), "ready")
            except BaseException:
                proc.kill()
                raise
        return proc

    def test_competing_process_and_orderly_release_keep_same_inode(self):
        proc = self.holder()
        inode = self.path.stat().st_ino
        code, _, err = self.attempt()
        self.assertNotEqual(code, 0)
        self.assertIn("another github-presence companion", err)
        proc.communicate("x", timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.attempt()[0], 0)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_forced_exit_releases_kernel_lock_without_deleting_file(self):
        proc = self.holder()
        proc.kill()
        proc.communicate(timeout=10)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.attempt()[0], 0)

    def test_exception_release_noninheritance_and_stale_file(self):
        self.path.write_text("stale process metadata is not a lease")
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with CompanionLock(self.path) as lock:
                self.assertFalse(os.get_inheritable(lock.fd))
                raise RuntimeError("synthetic")
        self.assertEqual(self.attempt()[0], 0)
        self.assertEqual(self.path.read_text(), "stale process metadata is not a lease")

    def test_windows_uses_nonblocking_same_byte_and_closes_failed_descriptor(self):
        calls = []
        def locking(fd, mode, size):
            calls.append((fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
        fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with patch.dict(sys.modules, msvcrt=fake):
            lock = CompanionLock(self.path)
            lock._lock = _windows_lock
            with lock:
                os.lseek(lock.fd, 10, os.SEEK_SET)
            self.assertEqual([(m, n, p) for _, m, n, p in calls], [(2, 1, 0), (0, 1, 0)])
            with self.assertRaises(OSError):
                os.fstat(calls[0][0])
            def refused(fd, mode, size):
                calls.append((fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
                raise OSError(errno.EACCES, "synthetic CRT contention")
            fake.locking = refused
            with self.assertRaises(CompanionAlreadyRunning):
                lock.__enter__()
            self.assertIsNone(lock.fd)
            with self.assertRaises(OSError):
                os.fstat(calls[-1][0])

    def test_non_contention_lock_error_is_not_reported_as_another_process(self):
        lock = CompanionLock(self.path)
        with patch.object(lock, "_lock", side_effect=OSError(errno.EBADF, "synthetic bad descriptor")):
            with self.assertRaises(OSError) as failure:
                lock.__enter__()
            self.assertEqual(failure.exception.errno, errno.EBADF)

    def test_companion_import_and_windows_backend_selection_without_fcntl(self):
        spec = importlib.util.spec_from_file_location("companion_lock_import_check", ROOT / "scripts/companion.py")
        module = importlib.util.module_from_spec(spec)
        original = __import__
        def reject_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise AssertionError("Windows must not import fcntl")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=reject_fcntl):
            spec.loader.exec_module(module)
            with patch("gh_presence.locking.os.name", "nt"):
                self.assertIs(CompanionLock(self.path)._lock, _windows_lock)

    def test_real_disabled_companion_excludes_second_before_store_recovery(self):
        proc = subprocess.Popen([sys.executable, "-B", str(ROOT / "scripts/companion.py")],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=self.env, cwd=self.tmp.name)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)
        self.addCleanup(cleanup)
        ready = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc.poll() is None:
            store = Store(self.tmp.name, readonly=True)
            try:
                ready = store.meta("runtime", {}).get("state") == "disabled"
            finally:
                store.close()
            if ready:
                break
            time.sleep(0.02)
        self.assertTrue(ready, "disabled companion did not reach locked runtime")
        second = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/companion.py")],
                                capture_output=True, text=True, env=self.env, cwd=self.tmp.name, timeout=10)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("another github-presence companion", second.stderr)
        proc.terminate()
        proc.communicate(timeout=10)
        if os.name != "nt":
            self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.attempt()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
