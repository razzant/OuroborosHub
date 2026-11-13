"""RTP packets (RFC 3550), DTMF named events (RFC 4733) and a symmetric RTP endpoint.

The endpoint sends from the same UDP socket it receives on (symmetric RTP,
RFC 4961), so a provider behind or in front of NAT can latch onto it. Media
from the address the SDP named is always accepted and always becomes the send
target again. Media from another address (a gateway behind NAT announcing a
private address) is followed only while the SDP's address has sent nothing and
nothing else was followed yet, only from a source the call allows (the SDP's
host on another port, or what ``may_follow`` accepts: the call's own SIP
addresses), and only after ``LATCH_PACKETS`` consecutive packets from that one
source with one SSRC. The address followed then stays the only other one taken
until the SDP names a new address: a stray or hostile sender can neither
capture the stream nor take it over later.
"""

from __future__ import annotations

import asyncio
import collections
import random
import struct
import time
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Tuple

RTP_VERSION = 2
HEADER = struct.Struct("!BBHII")

DTMF_EVENTS = "0123456789*#ABCD"
DTMF_VOLUME = 10           # dBm0 below max, a typical value
DTMF_DIGIT_MS = 160         # tone length per digit
DTMF_GAP_MS = 80            # pause between digits
LATCH_PACKETS = 3           # consecutive packets from one other source before following it


@dataclass(frozen=True)
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False


def build_packet(packet: RtpPacket) -> bytes:
    first = RTP_VERSION << 6
    second = (0x80 if packet.marker else 0) | (packet.payload_type & 0x7F)
    return HEADER.pack(first, second, packet.sequence & 0xFFFF, packet.timestamp & 0xFFFFFFFF,
                       packet.ssrc & 0xFFFFFFFF) + packet.payload


def parse_packet(data: bytes) -> Optional[RtpPacket]:
    """Parse one RTP packet; ``None`` for anything that is not RTP version 2 (RTCP included)."""
    if len(data) < HEADER.size:
        return None
    first, second, sequence, timestamp, ssrc = HEADER.unpack_from(data)
    if first >> 6 != RTP_VERSION:
        return None
    payload_type = second & 0x7F
    if 72 <= payload_type <= 76:  # RTCP multiplexed on the RTP port
        return None
    offset = HEADER.size + 4 * (first & 0x0F)
    if first & 0x10:  # header extension
        if len(data) < offset + 4:
            return None
        _, words = struct.unpack_from("!HH", data, offset)
        offset += 4 + 4 * words
    end = len(data)
    if first & 0x20:  # padding
        if end <= offset:
            return None
        end -= data[-1]
    if end < offset:
        return None
    return RtpPacket(payload_type, sequence, timestamp, ssrc, data[offset:end], bool(second & 0x80))


def dtmf_payload(event: int, end: bool, volume: int, duration: int) -> bytes:
    return struct.pack("!BBH", event & 0xFF, (0x80 if end else 0) | (volume & 0x3F), duration & 0xFFFF)


def parse_dtmf(payload: bytes) -> Optional[Tuple[int, bool, int, int]]:
    """(event, end, volume, duration) of an RFC 4733 named-event payload."""
    if len(payload) < 4:
        return None
    event, flags, duration = struct.unpack_from("!BBH", payload)
    return event, bool(flags & 0x80), flags & 0x3F, duration


def dtmf_event_code(digit: str) -> int:
    digit = digit.upper()
    if len(digit) != 1 or digit not in DTMF_EVENTS:
        raise ValueError(f"not a DTMF digit: {digit!r}")
    return DTMF_EVENTS.index(digit)


def dtmf_packets(digit: str, *, payload_type: int, ssrc: int, sequence: int, timestamp: int,
                 duration_ms: int = DTMF_DIGIT_MS, frame_ms: int = 20, rate: int = 8000) -> List[RtpPacket]:
    """The packets of one digit: growing duration every frame, then three end packets.

    All packets carry the event's start timestamp; the first one has the marker bit.
    """
    event = dtmf_event_code(digit)
    step = rate * frame_ms // 1000
    total = rate * duration_ms // 1000
    packets = []
    duration = step
    seq = sequence
    while duration < total:
        packets.append(RtpPacket(payload_type, seq, timestamp, ssrc,
                                 dtmf_payload(event, False, DTMF_VOLUME, duration), marker=not packets))
        seq += 1
        duration += step
    for _ in range(3):
        packets.append(RtpPacket(payload_type, seq, timestamp, ssrc,
                                 dtmf_payload(event, True, DTMF_VOLUME, total), marker=not packets))
        seq += 1
    return packets


class DtmfReceiver:
    """Reports each received digit once, whichever of its (retransmitted, reordered) packets arrives first.

    An event is identified by its start timestamp (RFC 4733 §2.5.1.2); the last few are remembered, so a late
    end packet of the previous digit arriving after the next digit began is not reported again.
    """

    def __init__(self, remember: int = 8) -> None:
        self._seen: Deque[Tuple[int, int]] = collections.deque(maxlen=remember)

    def feed(self, packet: RtpPacket) -> Optional[str]:
        parsed = parse_dtmf(packet.payload)
        if parsed is None:
            return None
        event, _end, _volume, _duration = parsed
        key = (packet.ssrc, packet.timestamp)
        if key in self._seen:
            return None
        self._seen.append(key)
        if event < len(DTMF_EVENTS):
            return DTMF_EVENTS[event]
        return None


class RtpEndpoint(asyncio.DatagramProtocol):
    """One call's RTP socket. ``on_packet(packet, address)`` sees every parsed RTP packet."""

    def __init__(self, on_packet: Callable[[RtpPacket], None]):
        self._on_packet = on_packet
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.remote: Optional[Tuple[str, int]] = None
        # Whether media from an address other than the SDP's may be followed (a far end behind NAT); None: only the
        # SDP's own host on another port.
        self.may_follow: Optional[Callable[[Tuple[str, int]], bool]] = None
        self._signalled: Optional[Tuple[str, int]] = None
        self._heard_signalled = False
        self._followed: Optional[Tuple[str, int]] = None  # the other address followed for the current SDP
        self._candidate: Optional[Tuple[Tuple[str, int], int]] = None  # (source, ssrc) being counted
        self._candidate_count = 0
        self.last_received = 0.0
        self.ssrc = random.getrandbits(32)
        self.sequence = random.getrandbits(16)
        self.timestamp = random.getrandbits(32)
        self.stats = {"sent": 0, "received": 0, "foreign": 0, "latched": 0}

    @classmethod
    async def open(cls, host: str, on_packet: Callable[[RtpPacket], None], *,
                   port_range: Tuple[int, int] = (16000, 32000), attempts: int = 50) -> "RtpEndpoint":
        loop = asyncio.get_running_loop()
        last_error: Optional[OSError] = None
        for _ in range(attempts):
            port = random.randrange(port_range[0], port_range[1]) & ~1  # RTP on an even port
            try:
                _, protocol = await loop.create_datagram_endpoint(lambda: cls(on_packet), local_addr=(host, port))
                return protocol
            except OSError as exc:
                last_error = exc
        raise OSError(f"no free RTP port on {host}: {last_error}")

    @property
    def local_port(self) -> int:
        return self.transport.get_extra_info("sockname")[1] if self.transport else 0

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    @staticmethod
    def _valid_addr(address: Optional[Tuple[str, int]]) -> bool:
        return (isinstance(address, tuple) and len(address) == 2
                and isinstance(address[0], str) and bool(address[0])
                and type(address[1]) is int and 1 <= address[1] <= 65535)

    def set_remote(self, address: Tuple[str, int]) -> None:
        if not self._valid_addr(address):
            raise ValueError(f"bad RTP remote address: {address!r}")
        self._signalled = address  # a new SDP address: what was followed for the previous one is forgotten
        self.remote = address
        self._heard_signalled = False
        self._followed = None
        self._candidate, self._candidate_count = None, 0

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        packet = parse_packet(data)
        if packet is None:
            return
        address = (addr[0], addr[1])
        if address == self._signalled:
            self._heard_signalled = True
            self._candidate, self._candidate_count = None, 0
            if self.remote != address:
                self.remote = address  # the signalled address speaks: it is the target again
                self.stats["returned"] = self.stats.get("returned", 0) + 1
        elif address != self.remote:
            if not self._latch(address, packet.ssrc):
                self.stats["foreign"] += 1
                return
        self.stats["received"] += 1
        self.last_received = time.monotonic()
        self._on_packet(packet)

    def _latch(self, address: Tuple[str, int], ssrc: int) -> bool:
        """Follow a source other than the signalled one: only while nothing was followed for this SDP and the
        signalled address has not spoken, only a source the call allows, and only after a run of its packets
        that no other allowed source interrupted."""
        signalled = self._signalled
        if signalled is None or self._heard_signalled or self._followed is not None:
            return False  # pinned: a new address needs a new SDP
        if address[0] != signalled[0] and not (self.may_follow is not None and self.may_follow(address)):
            return False  # never a candidate, and no way to interrupt one
        if self._candidate == (address, ssrc):
            self._candidate_count += 1
        else:
            self._candidate, self._candidate_count = (address, ssrc), 1
        if self._candidate_count < LATCH_PACKETS:
            return False
        self.remote = self._followed = address  # latch (symmetric RTP behind NAT)
        self._candidate, self._candidate_count = None, 0
        self.stats["latched"] += 1
        return True

    def send_payload(self, payload_type: int, payload: bytes, *, samples: int = 160, marker: bool = False) -> None:
        if self.transport is None or not self._valid_addr(self.remote):
            return
        packet = RtpPacket(payload_type, self.sequence, self.timestamp, self.ssrc, payload, marker)
        try:
            self.transport.sendto(build_packet(packet), self.remote)
        except (OSError, ValueError, OverflowError):
            self.stats["send_errors"] = self.stats.get("send_errors", 0) + 1
            return
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp = (self.timestamp + samples) & 0xFFFFFFFF
        self.stats["sent"] += 1

    def send_packet(self, packet: RtpPacket) -> None:
        if self.transport is None or not self._valid_addr(self.remote):
            return
        try:
            self.transport.sendto(build_packet(packet), self.remote)
        except (OSError, ValueError, OverflowError):
            self.stats["send_errors"] = self.stats.get("send_errors", 0) + 1
            return
        self.stats["sent"] += 1

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
