"""Receive jitter buffer and the 20 ms media clock.

The jitter buffer holds incoming 20 ms line frames keyed by RTP sequence and
releases one per clock tick once ``target`` frames (40-60 ms) are buffered, so
network jitter becomes a fixed small delay. A missing frame is concealed by
repeating the last one at falling level, then silence. A buffer that stays more
than ``TRIM_MARGIN`` frames above its target for ``TRIM_AFTER_TICKS`` ticks (a
second: a sender whose clock runs fast, a burst after a stall, two streams at a
switch) drops its oldest frames back to the target; one that outgrows
``max_frames`` drops the oldest at once.

``Pacer`` ticks every 20 ms on the monotonic clock: tick ``n`` fires at
``start + n * period`` regardless of how long earlier ticks took, so RTP leaves
at a steady rate; ticks owed after a short stall are paid one per pass of the
event loop, so the packets received meanwhile keep up with them, and after a
stall longer than ``max_lag`` it resynchronises instead of firing a burst.
"""

from __future__ import annotations

import asyncio
import logging
import time
from array import array
from typing import Awaitable, Callable, Dict, Optional

log = logging.getLogger("telephony.jitter")

FRAME_SAMPLES = 160          # 20 ms at 8 kHz
FRAME_BYTES = FRAME_SAMPLES * 2
SILENCE_FRAME = bytes(FRAME_BYTES)
TRIM_MARGIN = 2              # frames above the target that count as a buffer grown too deep
TRIM_AFTER_TICKS = 50        # playout ticks (a second) it may stay that deep before it is trimmed


def _seq_delta(a: int, b: int) -> int:
    """Signed distance b - a in 16-bit sequence space."""
    return ((b - a + 0x8000) & 0xFFFF) - 0x8000


class JitterBuffer:
    def __init__(self, target_frames: int = 3, max_frames: int = 15):
        if not 1 <= target_frames < max_frames:
            raise ValueError("need 1 <= target < max")
        self.target = target_frames
        self.max = max_frames
        self._frames: Dict[int, bytes] = {}
        self._next: Optional[int] = None
        self._playing = False
        self._last = SILENCE_FRAME
        self._concealed_run = 0
        self._deep_ticks = 0
        self.stats = {"received": 0, "late": 0, "duplicate": 0, "concealed": 0, "dropped": 0, "played": 0,
                      "trimmed": 0}

    def __len__(self) -> int:
        return len(self._frames)

    def reset(self) -> None:
        self._frames.clear()
        self._next = None
        self._playing = False
        self._concealed_run = 0
        self._deep_ticks = 0

    def put(self, sequence: int, pcm: bytes) -> None:
        """One decoded 20 ms frame (PCM16LE, 160 samples) with its RTP sequence."""
        if len(pcm) != FRAME_BYTES:
            # Other packet times are re-cut by the caller; keep the buffer frame-exact.
            raise ValueError("jitter buffer frames must be 20 ms at 8 kHz")
        sequence &= 0xFFFF
        if self._next is not None:
            delta = _seq_delta(self._next, sequence)
            if delta < 0:
                if delta < -1000:  # a sender restart (new stream): follow it
                    self.reset()
                elif self._playing:
                    self.stats["late"] += 1
                    return
                # before playout starts, an earlier frame joins the cushion and becomes the first to play
            elif delta > 1000:
                self.reset()
        if sequence in self._frames:
            self.stats["duplicate"] += 1
            return
        self._frames[sequence] = pcm
        self.stats["received"] += 1
        if self._next is None:
            self._next = sequence
        elif not self._playing and _seq_delta(sequence, self._next) > 0:
            self._next = sequence  # an earlier frame arrived before playout started
        if len(self._frames) > self.max:
            self._drop_oldest(len(self._frames) - self.max, "dropped")

    def _drop_oldest(self, count: int, stat: str) -> None:
        """Drop the ``count`` frames due first (playout skips ahead to the next one kept)."""
        for _ in range(count):
            del self._frames[min(self._frames, key=lambda s: _seq_delta(self._next, s))]
            self.stats[stat] += 1
        self._next = min(self._frames, key=lambda s: _seq_delta(self._next, s))

    def get(self) -> bytes:
        """The frame for this tick: buffered audio, a concealment frame, or silence."""
        if self._next is None:
            return SILENCE_FRAME
        if not self._playing:
            if len(self._frames) < self.target:
                return SILENCE_FRAME
            self._playing = True
            self._deep_ticks = 0
        if len(self._frames) > self.target + TRIM_MARGIN:
            self._deep_ticks += 1
            if self._deep_ticks >= TRIM_AFTER_TICKS:  # a second too deep: back to the target, latency with it
                self._drop_oldest(len(self._frames) - self.target, "trimmed")
                self._deep_ticks = 0
        else:
            self._deep_ticks = 0
        frame = self._frames.pop(self._next, None)
        self._next = (self._next + 1) & 0xFFFF
        if frame is None:
            if not self._frames:
                # Underrun: rebuild the cushion before playing again.
                self._playing = False
                self._next = None
            self.stats["concealed"] += 1
            self._concealed_run += 1
            if self._concealed_run <= 2:
                self._last = _attenuate(self._last, 0.5)
                return self._last
            return SILENCE_FRAME
        self._concealed_run = 0
        self._last = frame
        self.stats["played"] += 1
        return frame


def _attenuate(pcm: bytes, factor: float) -> bytes:
    samples = array("h", pcm)
    return array("h", (int(s * factor) for s in samples)).tobytes()


class Pacer:
    """Calls ``tick()`` every ``period`` seconds on the monotonic clock until stopped."""

    def __init__(self, tick: Callable[[], Awaitable[None]], *, period: float = 0.020, max_lag: float = 0.2,
                 clock: Callable[[], float] = time.monotonic):
        self._tick = tick
        self.period = period
        self.max_lag = max_lag
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self.stats = {"ticks": 0, "resyncs": 0, "max_late_ms": 0.0}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        start = self._clock()
        count = 0
        try:
            while True:
                due = start + count * self.period
                delay = due - self._clock()
                # Behind schedule (the loop stalled: a GC pause, a busy host) the owed ticks still yield one by one:
                # the RTP packets that queued up meanwhile are read one per loop pass, and a burst of ticks without
                # them would drain the jitter buffer into silence.
                await asyncio.sleep(delay if delay > 0 else 0)
                late = self._clock() - due
                if late > self.max_lag:
                    # Resynchronise rather than bursting the missed ticks out.
                    self.stats["resyncs"] += 1
                    start, count = self._clock(), 0
                    due = start
                    late = 0.0
                self.stats["max_late_ms"] = max(self.stats["max_late_ms"], late * 1000)
                self.stats["ticks"] += 1
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("media clock tick failed: %s", exc)
                count += 1
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # wait() returns once the clock has stopped; unlike ``await task`` it never swallows a cancellation
            # of the task that is stopping the clock.
            await asyncio.wait([task])
            if not task.cancelled() and task.exception() is not None:
                log.warning("media clock ended with an error: %s", task.exception())
