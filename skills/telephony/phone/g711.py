"""G.711 mu-law (PCMU) and A-law (PCMA) codecs in pure Python.

The algorithms are the classic Sun/CCITT reference ones that CPython's former
``audioop`` module used (``lin2ulaw``/``ulaw2lin``/``lin2alaw``/``alaw2lin`` with
width 2), so the bytes are identical to what that module produced. ``audioop``
itself is not used: it is gone from Python 3.13.

Encoding runs through a 65536-entry table built once, decoding through a
256-entry table; both work on PCM16 little-endian mono bytes.
"""

from __future__ import annotations

import sys
from array import array

_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
_BIAS = 0x84
_CLIP = 32635


def _search(value: int, table) -> int:
    for index, end in enumerate(table):
        if value <= end:
            return index
    return len(table)


def _linear14_to_ulaw(pcm: int) -> int:
    if pcm < 0:
        pcm, mask = -pcm, 0x7F
    else:
        mask = 0xFF
    if pcm > _CLIP:
        pcm = _CLIP
    pcm += _BIAS >> 2
    seg = _search(pcm, _SEG_UEND)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm >> (seg + 1)) & 0xF)) ^ mask


def _ulaw_to_linear16(value: int) -> int:
    value = ~value & 0xFF
    t = ((value & 0x0F) << 3) + _BIAS
    t <<= (value & 0x70) >> 4
    return (_BIAS - t) if value & 0x80 else (t - _BIAS)


def _linear13_to_alaw(pcm: int) -> int:
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = _search(pcm, _SEG_AEND)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    aval |= ((pcm >> 1) if seg < 2 else (pcm >> seg)) & 0x0F
    return aval ^ mask


def _alaw_to_linear16(value: int) -> int:
    value ^= 0x55
    t = (value & 0x0F) << 4
    seg = (value & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if value & 0x80 else -t


def _signed(index: int) -> int:
    return index - 0x10000 if index & 0x8000 else index


# Index by the unsigned 16-bit pattern of a sample (sample & 0xFFFF).
_ULAW_ENCODE = bytes(_linear14_to_ulaw(_signed(i) >> 2) for i in range(0x10000))
_ALAW_ENCODE = bytes(_linear13_to_alaw(_signed(i) >> 3) for i in range(0x10000))
ULAW_DECODE = tuple(_ulaw_to_linear16(i) for i in range(256))
ALAW_DECODE = tuple(_alaw_to_linear16(i) for i in range(256))

# One encoded byte that decodes to (near) silence, for comfort frames.
ULAW_SILENCE = _ULAW_ENCODE[0]
ALAW_SILENCE = _ALAW_ENCODE[0]


def _samples(pcm: bytes) -> array:
    if len(pcm) % 2:
        raise ValueError("PCM16 needs an even number of bytes")
    samples = array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _pcm(samples: array) -> bytes:
    if sys.byteorder != "little":
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def encode(pcm: bytes, codec: str) -> bytes:
    """PCM16LE mono -> G.711 bytes; ``codec`` is ``PCMU`` or ``PCMA``."""
    table = _ULAW_ENCODE if codec == "PCMU" else _ALAW_ENCODE if codec == "PCMA" else None
    if table is None:
        raise ValueError(f"unsupported G.711 codec {codec!r}")
    return bytes([table[s & 0xFFFF] for s in _samples(pcm)])


def decode(data: bytes, codec: str) -> bytes:
    """G.711 bytes -> PCM16LE mono."""
    table = ULAW_DECODE if codec == "PCMU" else ALAW_DECODE if codec == "PCMA" else None
    if table is None:
        raise ValueError(f"unsupported G.711 codec {codec!r}")
    return _pcm(array("h", [table[b] for b in data]))


def silence(codec: str, samples: int = 160) -> bytes:
    return bytes([ULAW_SILENCE if codec == "PCMU" else ALAW_SILENCE]) * samples
