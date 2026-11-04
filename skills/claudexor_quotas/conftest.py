"""Helpers shared by the widget tests: a memory guard for every Node run, the browser fixture and the place of the Russian table.

The widget tests start Node with ``subprocess.run([node, "-e", harness])``. A failing comparison of fake DOM nodes can print without end (a node holds its
parent, its children and its document, which hold it back), and the process then grows outside the V8 heap, where --max-old-space-size does not reach and a
timeout comes too late. So every such run is watched from a thread: twenty times a second it reads the process's physical footprint (the number the system
kills by, proc_pid_rusage ri_phys_footprint; the resident size is read too), and past LIMIT the process is killed at once and the test fails with the size it
reached. The guard stands for the run of the tests alone (pytest_configure puts it in place of subprocess.run, pytest_unconfigure takes it away).

Debug runs go under the same guard:  python3 conftest.py -- node some_debug.js   (exit code 137 when the guard killed it).
"""
import ctypes
import os
import subprocess
import sys
import threading
from pathlib import Path

try:
    import pytest
except ImportError:  # the command line at the end runs without it
    pytest = None

LIMIT = int(os.environ.get("QUOTAS_NODE_LIMIT_BYTES", 1 << 30))
TICK = 0.05

# The Russian translation file (ru.json) lives outside the skill. The one place that reads where it is: the Russian checks take RU_JSON
# from here, and skip with RU_JSON_SKIP when QUOTAS_RU_JSON is not set or names no file.
_ru_json = os.environ.get("QUOTAS_RU_JSON", "")
RU_JSON = Path(_ru_json) if _ru_json and Path(_ru_json).is_file() else None
RU_JSON_SKIP = "the Russian translation file is not given: set QUOTAS_RU_JSON to the path of ru.json"

if pytest is not None:
    def launch_engine(playwright, engine):
        """The browser of an engine (chromium, webkit) started by Playwright; Chromium falls back to the installed Chrome where
        the bundled build is missing, and the test is skipped where this machine has no build of the engine."""
        errors = []
        for options in ({}, {"channel": "chrome"}) if engine == "chromium" else ({},):
            try:
                return getattr(playwright, engine).launch(**options)
            except Exception as exc:  # no such browser build for Playwright here
                errors.append(f"{options or 'bundled'}: {type(exc).__name__}")
        pytest.skip(f"no {engine} build for Playwright on this machine (" + "; ".join(errors) + ")")

    @pytest.fixture(params=["chromium", "webkit"])
    def browser(request):
        """A browser of each engine in turn, closed after the test (the tests that drive Playwright take it by name)."""
        sync_api = pytest.importorskip("playwright.sync_api")
        with sync_api.sync_playwright() as playwright:
            found = launch_engine(playwright, request.param)
            try:
                yield found
            finally:
                found.close()

try:
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
except OSError:  # not macOS: read the resident size from ps instead
    _libproc = None

_real_run = subprocess.run


def footprint(pid):
    """(physical footprint, resident size) of a process in bytes, or None once it is gone."""
    if _libproc is not None:
        buf = ctypes.create_string_buffer(512)
        if _libproc.proc_pid_rusage(pid, 2, buf) != 0:  # RUSAGE_INFO_V2
            return None
        raw = buf.raw
        return int.from_bytes(raw[72:80], "little"), int.from_bytes(raw[64:72], "little")
    out = _real_run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return (int(out) * 1024,) * 2 if out else None


class NodeOverMemory(MemoryError):
    pass


def _is_node(cmd):
    return isinstance(cmd, (list, tuple)) and bool(cmd) and os.path.basename(str(cmd[0])).startswith("node")


def guarded_run(*popenargs, **kwargs):
    """subprocess.run with the memory guard for Node; any other command runs as it is."""
    cmd = popenargs[0] if popenargs else kwargs.get("args")
    if not _is_node(cmd):
        return _real_run(*popenargs, **kwargs)
    timeout = kwargs.pop("timeout", None)
    check = kwargs.pop("check", False)
    data = kwargs.pop("input", None)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if data is not None:
        kwargs["stdin"] = subprocess.PIPE
    stop = threading.Event()
    over = {}
    with subprocess.Popen(*popenargs, **kwargs) as proc:
        def watch():
            while not stop.wait(TICK):
                size = footprint(proc.pid)
                if size is None:
                    return
                if max(size) > LIMIT:
                    over["bytes"] = max(size)
                    proc.kill()
                    return
        threading.Thread(target=watch, daemon=True).start()
        try:
            out, err = proc.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            stop.set()
    if over:
        raise NodeOverMemory("node grew past %d MiB (reached %d MiB) and was killed: %s"
                             % (LIMIT >> 20, over["bytes"] >> 20, " ".join(str(c) for c in cmd[:2])[:120]))
    done = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check:
        done.check_returncode()
    return done


if pytest is not None:
    def pytest_configure(config):
        """Every test module reads subprocess.run at the call, so one swap here covers them all, for the time of the run."""
        subprocess.run = guarded_run

    def pytest_unconfigure(config):
        subprocess.run = _real_run


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not _is_node(argv):
        sys.exit("usage: python3 conftest.py -- node <file.js | -e code> ...")
    try:
        sys.exit(guarded_run(argv).returncode)
    except NodeOverMemory as error:
        print(error, file=sys.stderr)
        sys.exit(137)
