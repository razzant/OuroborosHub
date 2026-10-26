"""The cross-process throttle: spacing, lock scope and deadline behaviour (offline)."""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import threading
import time

import pytest

TRANSPORT = pathlib.Path(__file__).resolve().parents[1] / "transport.py"
spec = importlib.util.spec_from_file_location("science_search_transport_under_test", TRANSPORT)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)

WORKER = """
import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location("t", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
throttle, deadline = module.Throttle(pathlib.Path(sys.argv[2])), module.Deadline(60)
for _ in range(int(sys.argv[3])):
    with throttle.slot("arxiv", float(sys.argv[4]), deadline):
        print(repr(time.time()), flush=True)
"""


def test_two_processes_share_one_request_pace(tmp_path):
    interval, per_process = 0.25, 4
    # -B: isolated mode ignores PYTHONDONTWRITEBYTECODE, and the workers must not leave bytecode beside the skill.
    command = [sys.executable, "-I", "-B", "-c", WORKER, str(TRANSPORT), str(tmp_path / "throttle"), str(per_process),
               str(interval)]
    workers = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outputs = [worker.communicate(timeout=60) for worker in workers]
    assert all(worker.returncode == 0 for worker in workers), outputs
    stamps = sorted(float(line) for stdout, _ in outputs for line in stdout.split())
    assert len(stamps) == 2 * per_process
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= interval - 0.02, gaps
    assert float((tmp_path / "throttle" / "arxiv.lock").read_text()) >= stamps[-1]


def test_lock_covers_interval_and_send_only(tmp_path):
    throttle, deadline = transport.Throttle(tmp_path / "throttle"), transport.Deadline(30)
    inside, released = threading.Event(), threading.Event()

    def holder():
        with throttle.slot("arxiv", 0.0, deadline):
            inside.set()
            released.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert inside.wait(2)
    with open(tmp_path / "throttle" / "arxiv.lock", "r+", encoding="utf-8") as handle:
        assert transport._try_lock(handle) is False  # busy while the request is in flight
        released.set()
        thread.join(2)
        assert transport._try_lock(handle) is True  # free as soon as the slot exits
        transport._unlock(handle)
    previous = float((tmp_path / "throttle" / "arxiv.lock").read_text(encoding="utf-8"))  # stored as the holder ended
    with throttle.slot("arxiv", 0.3, deadline):
        sent = time.time()
    # The next request goes out a full interval after the previous one, however late this test got scheduled.
    assert sent - previous >= 0.3 - 0.005, (previous, sent)


def test_waiting_past_the_deadline_is_a_typed_failure_and_releases_the_lock(tmp_path):
    throttle = transport.Throttle(tmp_path / "throttle")
    with throttle.slot("ncbi", 0.0, transport.Deadline(5)):
        pass
    with pytest.raises(transport.SourceError) as refusal:
        with throttle.slot("ncbi", 5.0, transport.Deadline(0.05)):
            pytest.fail("the slot must not open when the wait exceeds the deadline")
    assert refusal.value.code == "deadline_exceeded"
    with throttle.slot("ncbi", 0.0, transport.Deadline(5)):
        pass  # the lock was released by the refused attempt
    (tmp_path / "throttle" / "ncbi.lock").write_text("garbage", encoding="utf-8")
    with throttle.slot("ncbi", 0.0, transport.Deadline(5)):
        pass  # an unreadable timestamp counts as "never"
