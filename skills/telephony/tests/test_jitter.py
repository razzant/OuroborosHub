import asyncio
import struct
import time

import pytest

from conftest import run
from phone.jitter import FRAME_BYTES, SILENCE_FRAME, JitterBuffer, Pacer


def frame(n):
    return struct.pack("<160h", *([n] * 160))


def test_reorders_and_waits_for_target():
    jb = JitterBuffer(target_frames=3)
    for seq in (10, 12):
        jb.put(seq, frame(seq))
    assert jb.get() == SILENCE_FRAME  # cushion not full yet
    jb.put(11, frame(11))
    assert [jb.get() for _ in range(3)] == [frame(10), frame(11), frame(12)]


def test_conceals_loss_then_silence_and_rebuffers():
    jb = JitterBuffer(target_frames=2)
    for seq in (1, 2, 5):
        jb.put(seq, frame(1000))
    assert jb.get() == frame(1000) and jb.get() == frame(1000)
    concealed = jb.get()  # seq 3 lost: attenuated repeat
    assert struct.unpack("<h", concealed[:2])[0] == 500
    assert jb.get() != SILENCE_FRAME and jb.get() == frame(1000)  # 4 concealed, 5 played
    assert jb.stats["concealed"] == 2


def test_late_duplicate_wrap_and_overflow():
    jb = JitterBuffer(target_frames=2, max_frames=5)
    jb.put(65535, frame(1)); jb.put(0, frame(2)); jb.put(0, frame(2))
    assert jb.stats["duplicate"] == 1
    assert jb.get() == frame(1) and jb.get() == frame(2)
    jb.put(65534, frame(9))
    assert jb.stats["late"] == 1
    for seq in range(1, 12):
        jb.put(seq, frame(seq))
    assert len(jb) <= 5 and jb.stats["dropped"] >= 6


def test_pacer_keeps_20ms_without_drift():
    # A busy machine (a shared CI runner) may delay any tick; it must never move the grid. Tick n is due at
    # start + n * 20 ms: whatever came before, ticks of the second second still land on that grid, where a clock
    # counting 20 ms from the previous tick has shifted all of them by the slow tick's 50 ms and its own overhead.
    async def scenario():
        ticks = []

        async def tick():
            ticks.append(time.monotonic())
            if len(ticks) == 10:
                await asyncio.sleep(0.05)  # one slow tick must not shift the schedule

        pacer = Pacer(tick, max_lag=60.0)  # a starved process only delays ticks here: no resync moves the grid
        pacer.start()
        await asyncio.sleep(2.0)
        await pacer.stop()
        return ticks

    ticks = asyncio.run(scenario())
    assert len(ticks) >= 50  # running, however starved the process (100 are due)
    offsets = [at - n * 0.020 for n, at in enumerate(ticks)]  # where each tick sits on its 20 ms grid
    grid = min(offsets)  # no tick comes early, so the earliest shows the grid itself
    second_half = [offset - grid for offset in offsets[len(offsets) // 2:]]
    assert min(second_half) < 0.025, min(second_half)  # some tick of the last second is back on the grid


def test_stopping_the_media_clock_never_swallows_the_stoppers_own_cancellation():
    async def scenario():
        async def tick():
            pass

        pacer = Pacer(tick)
        pacer.start()
        await asyncio.sleep(0.05)
        stopper = asyncio.ensure_future(pacer.stop())
        await asyncio.sleep(0)  # the stopper now waits for the clock task to end
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper

    run(scenario())


def test_pacer_survives_tick_exception():
    async def scenario():
        ticks = []

        async def flaky():
            ticks.append(len(ticks))
            if len(ticks) == 1:
                raise RuntimeError("transient tick error")

        pacer = Pacer(flaky)
        pacer.start()
        try:
            for _ in range(30):
                if len(ticks) >= 3:
                    break
                await asyncio.sleep(0.01)
            assert len(ticks) >= 3
        finally:
            await pacer.stop()

    run(scenario())
