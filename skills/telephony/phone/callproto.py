"""The peer side of the live call protocol, version 1.

The companion runs outside the server's import path, so these constants mirror
``ouroboros/call_protocol.py`` and ``ouroboros/gateway/call_contracts.py`` of the
host; ``tests/test_callproto.py`` pins them to the host modules. Rules the host
hub enforces and this peer keeps: ``hello`` first; every microphone frame
carries the rate declared in ``hello`` and the hub's current output epoch
(learned from ``flush``; the previous epoch is still accepted); sequence
numbers only grow; ``played`` names an item id taken from a ``caption`` and
its played milliseconds; a peer control is at most 16 KiB of UTF-8.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, Dict

PROTOCOL_VERSION = 1
PEER_MEETING = "meeting"
FRAME_AUDIO_IN = 1
FRAME_AUDIO_OUT = 2
HEADER = struct.Struct("<2sBBHIIH")
HEADER_SIZE = HEADER.size
MAGIC = b"OC"
MAX_FRAME_PAYLOAD = 64 * 1024
MAX_CONTROL_BYTES = 16 * 1024
MIN_RATE, MAX_RATE = 8000, 192000
PEER_MESSAGE_TYPES = ("hello", "mute", "played", "context", "participant", "share", "bye")
SERVER_MESSAGE_TYPES = ("state", "activity", "turn", "caption", "flush")
STATE_LIVE = "live"
STATE_CLOSED = "closed"


@dataclass(frozen=True)
class AudioFrame:
    kind: int
    epoch: int
    seq: int
    rate: int
    stream: int
    pcm: bytes


def encode_frame(frame: AudioFrame) -> bytes:
    if len(frame.pcm) > MAX_FRAME_PAYLOAD or len(frame.pcm) % 2:
        raise ValueError("audio payload must be PCM16 and at most 64 KiB")
    return HEADER.pack(MAGIC, PROTOCOL_VERSION, frame.kind, frame.epoch & 0xFFFF, frame.seq & 0xFFFFFFFF,
                       frame.rate, frame.stream & 0xFFFF) + frame.pcm


def decode_frame(data: bytes) -> AudioFrame:
    if len(data) < HEADER_SIZE:
        raise ValueError("audio frame shorter than its header")
    magic, version, kind, epoch, seq, rate, stream = HEADER.unpack_from(data)
    pcm = data[HEADER_SIZE:]
    if magic != MAGIC or version != PROTOCOL_VERSION:
        raise ValueError("not a live call audio frame of this protocol version")
    if kind not in (FRAME_AUDIO_IN, FRAME_AUDIO_OUT) or len(pcm) % 2 or len(pcm) > MAX_FRAME_PAYLOAD:
        raise ValueError("malformed live call audio frame")
    return AudioFrame(kind, epoch, seq, rate, stream, pcm)


def encode_control(message: Dict[str, Any]) -> str:
    if message.get("type") not in PEER_MESSAGE_TYPES:
        raise ValueError(f"not a peer control: {message.get('type')!r}")
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise ValueError("peer control larger than the protocol allows")
    return text


def hello(rate: int, label: str, *, mic: bool = True, speaker: bool = True) -> Dict[str, Any]:
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ValueError("unsupported rate")
    return {"type": "hello", "protocol": PROTOCOL_VERSION, "role": PEER_MEETING, "mic": mic,
            "speaker": speaker, "video": False, "rate": rate, "label": label[:120]}


def bounded_text(text: str, limit: int = MAX_CONTROL_BYTES - 512) -> str:
    """Cut ``text`` so its control message stays inside the byte limit."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore") + "…"
