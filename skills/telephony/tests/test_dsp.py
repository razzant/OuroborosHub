import math
import struct

import pytest

from phone import dsp


def tone(freq, rate, n, amp=10000):
    return struct.pack(f"<{n}h", *[int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)])


def level_db(pcm, amp=10000):
    r = dsp.rms(pcm)
    return 20 * math.log10(max(r, 1e-3) / (amp / math.sqrt(2)))


def run(proc, pcm, chunk):
    out = b""
    for o in range(0, len(pcm), chunk):
        out += proc.process(pcm[o:o + chunk])
    return out


@pytest.mark.parametrize("factor", [2, 3])
@pytest.mark.parametrize("freq", [4600, 5000, 6000, 7900])
def test_downsampler_rejects_above_band(factor, freq):
    rate = 8000 * factor
    if freq >= rate / 2:
        pytest.skip("not representable")
    out = run(dsp.Downsampler(factor), tone(freq, rate, rate), 20 * rate // 1000 * 2)
    assert level_db(out[800:]) < -60, freq


@pytest.mark.parametrize("factor", [2, 3])
@pytest.mark.parametrize("freq", [300, 1000, 2500, 3300])
def test_downsampler_passes_speech_band(factor, freq):
    rate = 8000 * factor
    out = run(dsp.Downsampler(factor), tone(freq, rate, rate), 20 * rate // 1000 * 2)
    assert abs(level_db(out[800:])) < 0.5


def test_linear_resampler_would_alias_but_ours_does_not(host_doors):
    # The hub's own LinearResampler lets a 6 kHz tone fold to 2 kHz at 8 kHz; ours removes it.
    from ouroboros.call_peers import LinearResampler
    pcm = tone(6000, 24000, 24000)
    linear = LinearResampler(24000, 8000).feed(pcm)
    ours = run(dsp.Downsampler(3), pcm, 960)
    assert level_db(linear[800:]) > -20
    assert level_db(ours[800:]) < -60


@pytest.mark.parametrize("factor", [2, 3])
def test_upsampler_level_and_images(factor):
    rate = 8000 * factor
    up = run(dsp.Upsampler(factor), tone(1000, 8000, 8000), 320)
    assert len(up) == 8000 * factor * 2
    assert abs(level_db(up[4000:])) < 0.5
    # image of 1 kHz at 8k-1k = 7 kHz must be gone: measure with a narrow Goertzel
    samples = struct.unpack(f"<{len(up)//2}h", up)[2000:]
    def goertzel(f):
        w = 2 * math.pi * f / rate
        s1 = s2 = 0.0
        c = 2 * math.cos(w)
        for x in samples:
            s1, s2 = x + c * s1 - s2, s1
        return math.sqrt(max(s1 * s1 + s2 * s2 - c * s1 * s2, 0)) / len(samples)
    assert goertzel(7000) < goertzel(1000) * 1e-3


def test_chunking_is_seamless():
    pcm = tone(1234, 24000, 4800)
    whole = dsp.Downsampler(3).process(pcm)
    pieces = run(dsp.Downsampler(3), pcm, 194)  # odd chunk sizes
    assert whole == pieces
    pcm8 = tone(777, 8000, 1600)
    assert dsp.Upsampler(3).process(pcm8) == run(dsp.Upsampler(3), pcm8, 62)
