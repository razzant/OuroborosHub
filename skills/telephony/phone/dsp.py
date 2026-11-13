"""Telephone-band resampling between the 8 kHz line and the call hub's rate.

The hub's own resampler interpolates linearly with no anti-aliasing filter,
which folds everything above 4 kHz back into the band when it decimates
24 kHz speech to 8 kHz. This bridge therefore talks to the hub at 24 kHz (or
16 kHz) and does the 8 kHz conversion itself with a Kaiser-windowed FIR
low-pass (pass band to 3.4 kHz, stop band from 4.4 kHz, about 70 dB), run as
a polyphase filter so only the needed output samples are computed. Aliases
from the 4.0-4.4 kHz transition land at 3.6-4.0 kHz, above the telephone
band. Pure Python; state carries across calls, so streaming in 20 ms frames
equals one long conversion.
"""

from __future__ import annotations

import math
import sys
from array import array
from operator import mul
from typing import List

LINE_RATE = 8000
PASS_HZ = 3400.0
STOP_HZ = 4400.0
ATTENUATION_DB = 70.0


def _bessel_i0(x: float) -> float:
    total, term, k = 1.0, 1.0, 1
    while term > 1e-12 * total:
        term *= (x / (2.0 * k)) ** 2
        total += term
        k += 1
    return total


def design_lowpass(factor: int) -> List[float]:
    """Unity-DC-gain low-pass taps for the rate ``LINE_RATE * factor``."""
    if factor < 2:
        raise ValueError("factor must be at least 2")
    rate = LINE_RATE * factor
    width = 2.0 * math.pi * (STOP_HZ - PASS_HZ) / rate
    count = int(math.ceil((ATTENUATION_DB - 8.0) / (2.285 * width))) + 1
    count += (-count) % factor  # whole polyphase branches
    beta = 0.1102 * (ATTENUATION_DB - 8.7)
    cutoff = (PASS_HZ + STOP_HZ) / 2.0 / rate  # cycles per sample
    middle = (count - 1) / 2.0
    denominator = _bessel_i0(beta)
    taps = []
    for n in range(count):
        offset = n - middle
        ideal = 2.0 * cutoff if offset == 0 else math.sin(2.0 * math.pi * cutoff * offset) / (math.pi * offset)
        ratio = (2.0 * n / (count - 1)) - 1.0
        taps.append(ideal * _bessel_i0(beta * math.sqrt(max(0.0, 1.0 - ratio * ratio))) / denominator)
    scale = 1.0 / sum(taps)
    return [tap * scale for tap in taps]


def _to_samples(pcm: bytes) -> array:
    if len(pcm) % 2:
        raise ValueError("PCM16 needs an even number of bytes")
    samples = array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _to_pcm(values) -> bytes:
    out = array("h", [32767 if v > 32767 else -32768 if v < -32768 else int(round(v)) for v in values])
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


class Upsampler:
    """8 kHz -> ``8 kHz * factor`` with image rejection (zero-stuffing + low-pass, polyphase)."""

    def __init__(self, factor: int):
        self.factor = factor
        taps = design_lowpass(factor)
        branch = len(taps) // factor
        # Each phase convolves the last ``branch`` input samples; coefficients reversed so a
        # chronological window multiplies directly. Gain ``factor`` restores the level lost to zeros.
        self._phases = [[c * factor for c in reversed(taps[p::factor])] for p in range(factor)]
        self._branch = branch
        self._history = [0.0] * (branch - 1)

    @property
    def delay_out_samples(self) -> float:
        return (self._branch * self.factor - 1) / 2.0

    def process(self, pcm: bytes) -> bytes:
        samples = _to_samples(pcm)
        buf = self._history + list(samples)
        branch, phases = self._branch, self._phases
        out = []
        append = out.append
        for k in range(len(samples)):
            window = buf[k:k + branch]
            for phase in phases:
                append(sum(map(mul, phase, window)))
        self._history = buf[len(buf) - (branch - 1):] if branch > 1 else []
        return _to_pcm(out)

    def reset(self) -> None:
        self._history = [0.0] * (self._branch - 1)


class Downsampler:
    """``8 kHz * factor`` -> 8 kHz with anti-aliasing (low-pass, then keep every factor-th sample)."""

    def __init__(self, factor: int):
        self.factor = factor
        self._taps = list(reversed(design_lowpass(factor)))
        self.reset()

    @property
    def delay_in_samples(self) -> float:
        return (len(self._taps) - 1) / 2.0

    def reset(self) -> None:
        length = len(self._taps)
        self._buffer = [0.0] * (length - 1)
        self._base = -(length - 1)  # stream index of _buffer[0]
        self._next = 0              # stream index of the next output sample

    def process(self, pcm: bytes) -> bytes:
        samples = _to_samples(pcm)
        taps, length = self._taps, len(self._taps)
        buf = self._buffer
        buf.extend(samples)
        last = self._base + len(buf) - 1
        out = []
        append = out.append
        position = self._next
        base = self._base
        while position <= last:
            index = position - base
            append(sum(map(mul, taps, buf[index - length + 1:index + 1])))
            position += self.factor
        self._next = position
        keep_from = position - (length - 1) - base
        if keep_from > 0:
            del buf[:keep_from]
            self._base = base + keep_from
        return _to_pcm(out)


def rms(pcm: bytes) -> float:
    samples = _to_samples(pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))
