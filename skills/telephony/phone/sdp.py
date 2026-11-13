"""SDP offer/answer (RFC 3264, RFC 4566) for one G.711 audio stream with telephone-event.

Only what a telephone line needs: PCMU (0) and PCMA (8) at 8 kHz, 20 ms
packets, RFC 4733 ``telephone-event`` for DTMF, and the stream direction.
Either codec may come under a dynamic payload type (``a=rtpmap:96 PCMU/8000``,
RFC 3551 §3); ``Negotiated.receive`` is the payload types this side takes.
"""

from __future__ import annotations

import ipaddress
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .sip_message import ascii_int

STATIC_CODECS = {0: "PCMU", 8: "PCMA"}
CODEC_PT = {"PCMU": 0, "PCMA": 8}
DEFAULT_DTMF_PT = 101
TELEPHONE_EVENT = "telephone-event"
DIRECTIONS = ("sendrecv", "sendonly", "recvonly", "inactive")
_HOST = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*\.?$"
)


class SdpError(ValueError):
    pass


def _connection_ok(addr_type: str, address: str) -> bool:
    if not address:
        return False
    if addr_type == "IP4":
        try:
            ipaddress.IPv4Address(address)
            return True
        except ValueError:
            if ":" in address or all(part.isdigit() for part in address.rstrip(".").split(".")):
                return False
            return bool(_HOST.match(address))
    try:
        ipaddress.IPv6Address(address)
        return True
    except ValueError:
        if ":" in address or all(part.isdigit() for part in address.rstrip(".").split(".")):
            return False
        return bool(_HOST.match(address))


@dataclass
class MediaDescription:
    address: str
    port: int
    payload_types: List[int]
    rtpmap: Dict[int, str] = field(default_factory=dict)  # pt -> "name/rate"
    direction: str = "sendrecv"
    ptime: int = 20

    def codec_for(self, pt: int) -> Optional[str]:
        name = self.rtpmap.get(pt, "")
        if name:
            # <encoding name>/<clock rate>[/<channels>] (RFC 8866 §6.6): G.711 at 8000 Hz, with the channel count
            # left out (one) or given as 1; another clock rate, or stereo, is not this codec here.
            encoding, _, params = name.partition("/")
            rate, has_channels, channels = params.partition("/")
            if encoding.upper() in CODEC_PT and (not params or rate == "8000" and (not has_channels or channels == "1")):
                return encoding.upper()
            return None
        return STATIC_CODECS.get(pt)

    def dtmf_pt(self) -> Optional[int]:
        for pt in self.payload_types:
            name = self.rtpmap.get(pt, "").lower()
            if name in ("telephone-event/8000", "telephone-event/8000/1"):
                return pt
        return None


@dataclass
class Negotiated:
    codec: str
    payload_type: int        # what we send the codec with: the number the other side's SDP gave it
    dtmf_pt: Optional[int]   # what we send telephone-event with, likewise
    remote: Tuple[str, int]
    direction: str
    # payload type -> codec or TELEPHONE_EVENT, what this side takes in RTP; anything else is no audio of this
    # stream. Built by hand without it: the stream's own two numbers.
    receive: Optional[Dict[int, str]] = None

    def __post_init__(self) -> None:
        if self.receive is None:
            self.receive = {self.payload_type: self.codec}
            if self.dtmf_pt is not None:
                self.receive.setdefault(self.dtmf_pt, TELEPHONE_EVENT)


def offer_formats(codecs: Tuple[str, ...]) -> Dict[int, str]:
    """The payload types an offer of ours lists, in its order: the codecs at their static numbers, then
    telephone-event (``SessionDescription.offer`` writes exactly these)."""
    formats = {CODEC_PT[codec]: codec for codec in codecs}
    formats[DEFAULT_DTMF_PT] = TELEPHONE_EVENT
    return formats


def parse(text: str) -> MediaDescription:
    """The first audio stream of an SDP body."""
    session_address = ""
    media: Optional[MediaDescription] = None
    session_direction = "sendrecv"
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        key, value = line[0], line[2:]
        if key == "c":
            parts = value.split()
            if len(parts) != 3 or parts[0] != "IN" or parts[1] not in ("IP4", "IP6"):
                raise SdpError(f"bad connection line: {value}")
            address = parts[2].split("/")[0]
            if not _connection_ok(parts[1], address):
                raise SdpError(f"bad connection address in SDP: {value}")
            if media is None:
                session_address = address
            else:
                media.address = address
        elif key == "m":
            if media is not None:
                break  # only the first audio stream
            parts = value.split()
            if len(parts) < 4 or parts[0] != "audio":
                continue
            port = ascii_int(parts[1].split("/")[0])
            if port is None or not 1 <= port <= 65535:
                raise SdpError(f"bad media line: {value}")
            pts = [pt for p in parts[3:] if (pt := ascii_int(p)) is not None and 0 <= pt <= 127]
            if not pts:
                raise SdpError(f"bad media line: {value}")
            media = MediaDescription(session_address, port, pts, direction=session_direction)
        elif key == "a":
            name, _, rest = value.partition(":")
            if name in DIRECTIONS:
                if media is None:
                    session_direction = name
                else:
                    media.direction = name
            elif media is not None and name == "rtpmap":
                pt_text, _, encoding = rest.partition(" ")
                pt = ascii_int(pt_text)
                if pt is not None and 0 <= pt <= 127:
                    media.rtpmap[pt] = encoding.strip()
            elif media is not None and name == "ptime":
                ptime = ascii_int(rest.strip())
                if ptime is not None and ptime > 0:
                    media.ptime = ptime
    if media is None:
        raise SdpError("no audio stream in SDP")
    if not media.address:
        raise SdpError("no connection address in SDP")
    return media


def negotiate(remote: MediaDescription, preferred: Tuple[str, ...] = ("PCMU", "PCMA"), *,
              offered: Optional[Dict[int, str]] = None) -> Negotiated:
    """Pick the codec: the remote's order for an offer we answer; the first common one otherwise.

    Each side's SDP names the payload types it expects to receive (RFC 3264 §5.1, §6.1). Answering an offer
    (``offered`` None), our answer repeats the offer's numbers for the codec and telephone-event, and those are
    all we take. Taking an answer to our offer (``offered``: what it listed), the other side sends with our
    numbers for formats accepted in the answer; its answer's own numbers for those formats are taken as well
    when they mean nothing else to us
    (an answerer that renumbered and sends as it receives). A number the two SDPs give different meanings is
    taken for neither, and an answer that leaves no codec number both sides read alike is refused: the call
    would be one-way."""
    dtmf = remote.dtmf_pt()
    for pt in remote.payload_types:
        codec = remote.codec_for(pt)
        if codec not in preferred:
            continue
        if offered is None:
            receive = {pt: codec}
            if dtmf is not None:
                receive[dtmf] = TELEPHONE_EVENT
        else:
            answered = {other: TELEPHONE_EVENT if other == dtmf else remote.codec_for(other)
                        for other in remote.payload_types}
            common = set(offered.values()) & set(answered.values())
            receive = {other: kind for other, kind in offered.items() if kind in common}
            for other, kind in answered.items():
                if other in offered and offered[other] != kind:
                    receive.pop(other, None)  # also reject a number remapped to an unsupported format
                elif kind in common:
                    receive[other] = kind
            if all(kind == TELEPHONE_EVENT for kind in receive.values()):
                raise SdpError("the answer gives every codec number of our offer another meaning")
        return Negotiated(codec, pt, dtmf, (remote.address, remote.port), remote.direction, receive)
    raise SdpError("no common codec (PCMU/PCMA)")


def _answer_direction(offered: str) -> str:
    return {"sendonly": "recvonly", "recvonly": "sendonly", "inactive": "inactive"}.get(offered, "sendrecv")


class SessionDescription:
    """Our side of the session: keeps origin id and bumps the version on each change."""

    def __init__(self, address: str, port: int, *, username: str = "-"):
        self.address = address
        self.port = port
        self.username = username
        self.session_id = random.randint(10**9, 10**10)
        self.version = int(time.time()) & 0x7FFFFFFF

    def _body(self, codecs: List[Tuple[int, str]], dtmf_pt: Optional[int], direction: str) -> str:
        ip = "IP6" if ":" in self.address else "IP4"
        pts = [str(pt) for pt, _ in codecs] + ([str(dtmf_pt)] if dtmf_pt is not None else [])
        lines = [
            "v=0",
            f"o={self.username} {self.session_id} {self.version} IN {ip} {self.address}",
            "s=-",
            f"c=IN {ip} {self.address}",
            "t=0 0",
            f"m=audio {self.port} RTP/AVP {' '.join(pts)}",
        ]
        for pt, name in codecs:
            lines.append(f"a=rtpmap:{pt} {name}/8000")
        if dtmf_pt is not None:
            lines.append(f"a=rtpmap:{dtmf_pt} telephone-event/8000")
            lines.append(f"a=fmtp:{dtmf_pt} 0-16")
        lines.append("a=ptime:20")
        lines.append(f"a={direction}")
        return "\r\n".join(lines) + "\r\n"

    def offer(self, codecs: Tuple[str, ...] = ("PCMU", "PCMA"), direction: str = "sendrecv") -> str:
        formats = offer_formats(codecs)
        return self._body([(pt, name) for pt, name in formats.items() if name != TELEPHONE_EVENT], DEFAULT_DTMF_PT,
                          direction)

    def answer(self, negotiated: Negotiated) -> str:
        return self._body([(negotiated.payload_type, negotiated.codec)], negotiated.dtmf_pt,
                          _answer_direction(negotiated.direction))

    def bump(self) -> None:
        self.version += 1
