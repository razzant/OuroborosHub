"""A small SIP user agent over UDP on asyncio (RFC 3261 subset for one phone line).

What it does: REGISTER with digest auth (MD5, SHA-256) and refresh, NAT
keepalive and ``rport`` learning (RFC 3581); outgoing INVITE with provisional
responses, early media SDP, digest retries across 407 then 401, CANCEL while
ringing, ACK (again for every retransmitted 2xx) and BYE, and ACK+BYE for a 2xx
that arrives after the call was cancelled or given up; incoming INVITE with
100/180, an answer when the owner of the line decides, or a rejection
(480/486/603) without answering, retransmitted until ACKed; CANCEL of a ringing
incoming call; re-INVITE/UPDATE (new remote address, hold, late offer), OPTIONS,
and DTMF over RFC 4733 or SIP INFO.

Transactions follow RFC 3261 §17: T1 = 500 ms, T2 = 4 s; an INVITE without any
provisional response times out after 64*T1 (Timer B), but once it rings only its
owner bounds the ringing (the call manager cancels at its ring limit;
``MAX_RINGING_SECONDS`` is a last safety net). A 2xx ends the INVITE client
transaction at once, so retransmitted 2xx responses reach the dialog.

Requests from outside a dialog are accepted only from the registrar, the
outbound proxy and the trusted sources (all their DNS addresses, re-read at each
registration, plus CIDR ranges); anything else, OPTIONS included, is dropped
without a reply. Requests inside a dialog are accepted from those addresses and
from the dialog's own remote party.

A forked INVITE keeps the first 2xx; every other 2xx, and any 2xx that cannot
be used, gets its ACK and a BYE.

Not done: TCP/TLS, SRTP, DNS SRV/NAPTR (give the registrar host and port),
PRACK (100rel is never offered). The SIP password is used only inside the
digest computation and is never logged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import logging
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import digest, g711
from .jitter import FRAME_BYTES, FRAME_SAMPLES, JitterBuffer
from .rtp import DTMF_EVENTS, DtmfReceiver, RtpEndpoint, RtpPacket, dtmf_packets, DTMF_GAP_MS
from .sdp import (TELEPHONE_EVENT, Negotiated, SdpError, SessionDescription, negotiate, offer_formats,
                  parse as parse_sdp)
from .sip_message import (NameAddr, SipMessage, SipParseError, SipUri, ascii_int, new_branch, new_call_id,
                          new_tag, parse as parse_sip, response_to, split_commas)

log = logging.getLogger("telephony.sip")

T1, T2, TRANSACTION_TIMEOUT = 0.5, 4.0, 32.0
KEEPALIVE_SECONDS = 25.0
TRUST_REFRESH_SECONDS = 300.0
MAX_RINGING_SECONDS = 300.0      # safety net only: the call manager cancels at its own ring limit
LATE_2XX_GUARD_SECONDS = 64.0    # how long an ended outgoing call still answers a late 2xx with ACK+BYE
STOP_SECONDS = 3.0               # a stop with no deadline of its own: its hang-ups and unregistration share it
STOP_FLOOR_SECONDS = 0.05        # even past its deadline, a stop lets its BYEs, CANCELs and REGISTER go out once
RX_PARTIAL_LIMIT = 16            # partly filled received frames kept; beyond it the oldest plays as it is
RX_PREVIOUS_SSRC_TICKS = 75      # 1.5 s after a new SSRC took over, packets of the one it replaced are dropped
RX_LIVE_TICKS = 4                # while the new one still sends: a packet within 80 ms (packet times up to 60 ms)
# A server transaction keeps its response 64*T1 = 32 s on UDP to answer retransmissions (RFC 3261 §17.2, Timers H, J).
# A one-call line sees a handful of requests in that time: its call's INVITE, CANCEL, BYE, re-INVITE or UPDATE, an
# INFO per key pressed, NOTIFYs, and the provider's OPTIONS pings seconds apart. Responses are a few hundred bytes to
# about 1.5 KB. These bounds hold that many times over; beyond them the oldest response is forgotten first, and a
# retransmission of its request is answered as a new request (an initial INVITE by the call it already started).
SERVER_RESPONSES_MAX = 256
SERVER_RESPONSES_MAX_BYTES = 256 * 1024
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, UPDATE, NOTIFY"
CALLER_NUMBER = re.compile(r"^[+0-9*#]{1,32}$")
Address = Tuple[str, int]


@dataclass
class SipConfig:
    registrar: str
    username: str
    password: str = ""
    registrar_port: int = 5060
    domain: str = ""
    auth_username: str = ""
    display_name: str = ""
    outbound_proxy: str = ""          # host[:port]; every request goes there when set
    register: bool = True
    expires: int = 600
    local_host: str = ""              # bind address; empty: the interface that routes to the registrar
    local_port: int = 0               # 0: an ephemeral port (scanners look at 5060)
    trusted_sources: Tuple[str, ...] = ()   # host names, IP addresses or CIDR ranges
    caller_id: str = ""               # user part of From for outgoing calls; default username
    dial_domain: str = ""             # host of the Request-URI for outgoing calls; default domain
    codecs: Tuple[str, ...] = ("PCMU", "PCMA")
    sdp_address: str = "auto"         # auto: the address the registrar saw (rport), else local
    user_agent: str = "Ouroboros-Telephony/0.2"
    rtp_port_range: Tuple[int, int] = (16000, 32000)

    @property
    def aor_domain(self) -> str:
        return self.domain or self.registrar


class SipError(RuntimeError):
    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(message or code)
        self.code, self.status = code, status


def _signed32(value: int) -> int:
    return ((value + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def _consume(future: asyncio.Future) -> None:
    """Mark a future's exception as retrieved (a transaction whose outcome nobody awaits)."""
    if not future.cancelled():
        future.exception()


def sanitize_number(raw: str) -> str:
    """A caller number as the network reported it, kept only when it is digits, ``+``, ``*`` or ``#``.

    Visual separators (``-``, ``.``, ``(``, ``)``) are dropped first; anything else makes the whole value
    ``unknown``: the user part of From can carry any text, and it must never reach the agent as words.
    """
    text = re.sub(r"[-.()]", "", str(raw or ""))
    return text if CALLER_NUMBER.match(text) else "unknown"


# ---------------------------------------------------------------------------------------------
# transport and transactions


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, ua: "SipUA"):
        self.ua = ua
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.ua._datagram(data, (addr[0], addr[1]))

    def error_received(self, exc) -> None:  # type: ignore[override]
        log.debug("SIP socket error: %s", exc)


class ClientTransaction:
    """RFC 3261 §17.1: INVITE (Timers A/B/D) and non-INVITE (Timers E/F/K) client transactions."""

    def __init__(self, ua: "SipUA", request: SipMessage, dest: Address,
                 on_provisional: Optional[Callable[[SipMessage], None]] = None):
        self.ua, self.request, self.dest = ua, request, dest
        self.method = request.method
        self.branch = request.top_via.branch
        self.on_provisional = on_provisional
        self.final: asyncio.Future = asyncio.get_running_loop().create_future()
        self.provisional_seen = False
        self._timer: Optional[asyncio.Task] = None

    @property
    def key(self) -> Tuple[str, str]:
        return self.branch, self.method

    def start(self) -> None:
        self.ua._client_tx[self.key] = self
        self.ua._send(self.request, self.dest)
        self._timer = self.ua._spawn(self._retransmit(), label=f"tx_{self.method}")

    def abandon(self) -> None:
        """Stop waiting (the owner gave the request up): no more retransmissions, the key is forgotten."""
        if self._timer is not None:
            self._timer.cancel()
        self.ua._client_tx.pop(self.key, None)
        if not self.final.done():
            self.final.set_exception(SipError("abandoned", f"{self.method} was abandoned", 0))
            _consume(self.final)

    def _ended_by_2xx(self) -> bool:
        return (self.method == "INVITE" and self.final.done() and not self.final.cancelled()
                and self.final.exception() is None and 200 <= self.final.result().status < 300)

    async def _retransmit(self) -> None:
        interval, started = T1, time.monotonic()
        try:
            while not self.final.done():
                await asyncio.sleep(interval)
                if self.final.done():
                    return
                elapsed = time.monotonic() - started
                if self.method == "INVITE":
                    if self.provisional_seen:
                        # Proceeding: Timer A and Timer B are stopped (§17.1.1.2). Only the owner ends ringing;
                        # this is the last safety net against a transaction kept forever.
                        if elapsed >= MAX_RINGING_SECONDS:
                            self.final.set_exception(SipError("ringing_too_long", "INVITE rang too long", 408))
                            return
                        interval = 1.0
                        continue
                    if elapsed >= TRANSACTION_TIMEOUT:  # Timer B
                        self.final.set_exception(SipError("timeout", "INVITE got no response", 408))
                        return
                    self.ua._send(self.request, self.dest)
                    interval *= 2
                else:
                    if elapsed >= TRANSACTION_TIMEOUT:  # Timer F
                        self.final.set_exception(SipError("timeout", f"{self.method} got no final response", 408))
                        return
                    self.ua._send(self.request, self.dest)
                    interval = T2 if self.provisional_seen else min(interval * 2, T2)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("SIP client transaction %s failed: %s", self.method, exc)
            if not self.final.done():
                self.final.set_exception(SipError("internal_error", str(exc), 0))
        finally:
            if not self._ended_by_2xx():
                # Timers D/K: keep absorbing retransmitted finals (and ACK them) for a while.
                asyncio.get_running_loop().call_later(TRANSACTION_TIMEOUT, self.ua._client_tx.pop, self.key, None)

    def receive(self, response: SipMessage) -> None:
        if response.status < 200:
            self.provisional_seen = True
            if self.on_provisional is not None:
                self.on_provisional(response)
            return
        if self.method == "INVITE" and response.status >= 300:
            self.ua._send(self._ack_for_failure(response), self.dest)  # also for retransmitted finals
        if self.method == "INVITE" and response.status < 300:
            # §17.1.1.2: a 2xx terminates the INVITE client transaction; its retransmissions go to the dialog.
            self.ua._client_tx.pop(self.key, None)
        if not self.final.done():
            self.final.set_result(response)

    def _ack_for_failure(self, response: SipMessage) -> SipMessage:
        request = self.request
        headers = [("Via", request.get("Via")), ("From", request.get("From")), ("To", response.get("To")),
                   ("Call-ID", request.call_id), ("CSeq", f"{request.cseq[0]} ACK"), ("Max-Forwards", "70")]
        headers += [("Route", r) for r in request.get_all("Route")]
        headers += _credentials(request)
        return SipMessage(method="ACK", uri=request.uri, headers=headers)


# ---------------------------------------------------------------------------------------------
# calls


@dataclass
class Dialog:
    call_id: str
    local_tag: str
    remote_tag: str = ""
    local_uri: str = ""      # name-addr without tag, always in <...>
    remote_uri: str = ""     # name-addr without tag, always in <...>
    remote_target: str = ""  # Contact URI
    route_set: List[str] = field(default_factory=list)
    local_cseq: int = 1
    remote_cseq: int = 0


def _name_addr(text: str) -> str:
    """``text`` as a name-addr in angle brackets without its tag, so a tag added later stays a header parameter."""
    parsed = NameAddr.parse(text)
    params = {k: v for k, v in parsed.params.items() if k != "tag"}
    return str(NameAddr(parsed.display, parsed.uri, params))


class SipCall:
    """One call leg. ``on_event(call, kind, data)`` reports ringing, early_media, answered, dtmf, hold, ended."""

    def __init__(self, ua: "SipUA", direction: str, remote_number: str):
        self.ua = ua
        self.id = secrets.token_hex(6)
        self.direction = direction
        self.remote_number = remote_number
        self.state = "new"
        self.on_event: Callable[["SipCall", str, dict], None] = lambda call, kind, data: None
        self.dialog: Optional[Dialog] = None
        self.invite: Optional[SipMessage] = None
        self.invite_dest: Optional[Address] = None
        self.invite_tx: Optional[ClientTransaction] = None
        self.local_sdp: Optional[SessionDescription] = None
        self.negotiated: Optional[Negotiated] = None
        self.rtp: Optional[RtpEndpoint] = None
        self.jitter = JitterBuffer()
        self.dtmf_rx = DtmfReceiver()
        self.ended: asyncio.Future = asyncio.get_running_loop().create_future()
        self.end_reason = ""
        self.end_status = 0
        self.on_hold = False
        self.answered_at: Optional[float] = None
        self.created_at = time.monotonic()
        self.peer_ips: Set[str] = set()          # where this dialog's requests may come from
        self._cancel_wanted = False
        self._cancel_sent = False
        self._answering_tag = ""
        self._sent_invites: Dict[int, SipMessage] = {}   # CSeq number -> each INVITE sent (its ACK copies credentials)
        self._stray_tags: Set[str] = set()
        self._stray_acks: Dict[str, Tuple[SipMessage, Address]] = {}
        self._bye_sent_tags: Set[str] = set()
        self._bye_task: Optional[asyncio.Task] = None     # the one BYE of this dialog, shared by every caller
        self._fixed_dest: Optional[Address] = None        # set when a 2xx's own routing could not be used
        self._ack_for_2xx: Optional[Tuple[SipMessage, Address]] = None
        self._pending_ok: Dict[int, Tuple[SipMessage, Address, asyncio.Task]] = {}
        self._awaiting_ack_answer = False
        self._awaiting_ack_cseq: Optional[int] = None
        self._incoming_invite: Optional[SipMessage] = None
        self._incoming_addr: Optional[Address] = None
        self._dest_cache: Dict[Tuple[str, int], Address] = {}
        self._dtmf_queue: List[str] = []
        self._dtmf_active: Optional[dict] = None
        self._info_keys: Optional[asyncio.Task] = None   # queued keys going by SIP INFO after an SDP change
        self._marker_next = True
        self._rx_ssrc: Optional[int] = None
        self._rx_base_ts: Optional[int] = None
        self._rx_last_ts: Optional[int] = None
        self._rx_next: Optional[int] = None     # sample just after the latest one received, from the base
        self._rx_partial: Dict[int, Tuple[bytearray, bytearray]] = {}  # frame index -> (pcm, filled mask)
        self._rx_prev_ssrc: Optional[int] = None  # the stream the current one replaced
        self._rx_ticks = 0                       # media clock ticks read so far (20 ms each)
        self._rx_switch_tick = 0                 # the tick of the last switch to a new SSRC
        self._rx_seen_tick = 0                   # the tick of the current stream's latest packet
        # rx_resets: how often the received stream restarted (a new SSRC or clock); playout goes on across them.
        # rx_stale_ssrc: packets of a replaced stream dropped while the new one played.
        self.stats = {"tx_frames": 0, "rx_packets": 0, "dtmf_sent": 0, "dtmf_received": 0, "rx_resets": 0,
                      "rx_stale_ssrc": 0}

    # -- media -----------------------------------------------------------------------------
    async def _open_rtp(self) -> None:
        self.rtp = await RtpEndpoint.open(self.ua.local_host, self._on_rtp, port_range=self.ua.config.rtp_port_range)
        self.rtp.may_follow = self._media_source_ok
        self.local_sdp = SessionDescription(self.ua.sdp_host(), self.rtp.local_port, username="ouroboros")

    def _media_source_ok(self, address: Address) -> bool:
        """Whether media from an address the SDP did not name may be followed: a far end behind NAT sends it from
        where its SIP comes from (this call's own signalling addresses), or from the provider's trusted hosts."""
        return address[0] in self.peer_ips or self.ua._trusted_source(address)

    def _apply_remote_sdp(self, body: bytes, *, answer_to_offer: bool) -> None:
        media = parse_sdp(body.decode("utf-8", "replace"))
        if ":" in media.address and media.address != "::" and ":" not in self.ua.local_host:
            raise SdpError("IPv6 media address on IPv4 RTP endpoint")
        preferred = self.ua.config.codecs if not answer_to_offer else ("PCMU", "PCMA")
        # Our own offer listed config.codecs at their static numbers: an answer to it is read against them.
        offered = None if answer_to_offer else offer_formats(self.ua.config.codecs)
        negotiated = negotiate(media, preferred, offered=offered)
        previous = self.negotiated
        was_on_hold = self.on_hold
        self.negotiated = negotiated
        self.on_hold = negotiated.direction in ("sendonly", "inactive") or media.address in ("0.0.0.0", "::")
        if self.rtp is not None and (previous is None or previous.remote != negotiated.remote):
            if media.address not in ("0.0.0.0", "::") and media.port:
                try:
                    self.rtp.set_remote(negotiated.remote)
                except ValueError as exc:
                    raise SdpError(str(exc)) from exc
        if self.state == "answered" and self.on_hold != was_on_hold:
            # Only a change of hold on a running call is news: early media and the answer itself are not, and a
            # direction change that keeps the stream two-way (recvonly from them) is no hold at all.
            self.on_event(self, "hold", {"on_hold": self.on_hold})

    def reset_media_input(self) -> None:
        """Forget everything received so far (early media before the answer, a previous stream)."""
        self.stats["early_media_dropped_frames"] = self.stats.get("early_media_dropped_frames", 0) + len(self.jitter)
        self._rx_ssrc, self._rx_base_ts, self._rx_last_ts, self._rx_next = None, None, None, None
        self._rx_prev_ssrc = None
        self._rx_partial.clear()
        self.jitter.reset()

    def _on_rtp(self, packet: RtpPacket) -> None:
        negotiated = self.negotiated
        if negotiated is None:
            return
        self.stats["rx_packets"] += 1
        codec = negotiated.receive.get(packet.payload_type)  # the numbers the SDPs gave, dynamic ones included
        if codec == TELEPHONE_EVENT:
            if packet.ssrc == self._rx_ssrc:
                self._rx_seen_tick = self._rx_ticks  # a key pressed: the stream still sends, with events for audio
            digit = self.dtmf_rx.feed(packet)
            if digit:
                self.stats["dtmf_received"] += 1
                self.on_event(self, "dtmf", {"digit": digit, "via": "rfc4733"})
            return
        if codec is None:
            self.stats["rx_unknown_pt"] = self.stats.get("rx_unknown_pt", 0) + 1  # never decoded as a guess
            return
        if packet.ssrc != self._rx_ssrc:
            if (packet.ssrc == self._rx_prev_ssrc and self._rx_ticks - self._rx_switch_tick <= RX_PREVIOUS_SSRC_TICKS
                    and self._rx_ticks - self._rx_seen_tick <= RX_LIVE_TICKS):
                # A late packet of the stream just replaced (a PBX sending both for a moment, a reordered
                # straggler) while the new one still flows: taking it would move the stream there and back, each
                # round trip a frame of delay for the rest of the call. A replaced stream that comes back alone
                # (the new one fell silent) is a switch again.
                self.stats["rx_stale_ssrc"] += 1
                return
            restart = True
        else:  # a timestamp jump of more than ten minutes from the previous packet: a new clock
            restart = abs(_signed32(packet.timestamp - self._rx_last_ts)) > 8000 * 600
        if restart:
            if self._rx_ssrc is None or self._rx_next is None:
                self._rx_base_ts = packet.timestamp  # the first stream: its clock starts the frame count
            else:
                # A new stream (a PBX rebuilding its bridge, a re-INVITE, a sender restarting its clock) goes on
                # at the next frame the last one would have filled: what is buffered still plays, no gap.
                self.stats["rx_resets"] += 1
                self._rx_base_ts = (packet.timestamp - self._rx_next) & 0xFFFFFFFF
            if packet.ssrc != self._rx_ssrc:
                self._rx_prev_ssrc, self._rx_switch_tick = self._rx_ssrc, self._rx_ticks
            self._rx_ssrc = packet.ssrc
        self._rx_last_ts = packet.timestamp
        self._rx_seen_tick = self._rx_ticks
        delta = _signed32(packet.timestamp - self._rx_base_ts)
        pcm = g711.decode(packet.payload, codec)
        self._place(delta, pcm)
        end = delta + len(pcm) // 2
        if self._rx_next is None or end > self._rx_next:
            self._rx_next = end

    def _place(self, sample: int, pcm: bytes) -> None:
        """Cut or join packets of any length (10, 20, 30, 60 ms ...) into 20 ms frames by their timestamp."""
        offset = 0
        newest = None
        while offset < len(pcm):
            index, within = divmod(sample + offset // 2, FRAME_SAMPLES)
            take = min(FRAME_SAMPLES - within, (len(pcm) - offset) // 2)
            frame, mask = self._rx_partial.get(index) or (bytearray(FRAME_BYTES), bytearray(FRAME_SAMPLES))
            frame[within * 2:(within + take) * 2] = pcm[offset:offset + take * 2]
            mask[within:within + take] = b"\x01" * take
            if all(mask):
                self._rx_partial.pop(index, None)
                self.jitter.put(index & 0xFFFF, bytes(frame))
            else:
                self._rx_partial[index] = (frame, mask)
            offset += take * 2
            newest = index if newest is None else max(newest, index)
        if newest is not None and len(self._rx_partial) > 4:
            for stale in [i for i in self._rx_partial if i < newest - 6]:
                frame, _ = self._rx_partial.pop(stale)  # a lost half: play what arrived, silence for the rest
                self.jitter.put(stale & 0xFFFF, bytes(frame))
        while len(self._rx_partial) > RX_PARTIAL_LIMIT:
            # Short packets whose timestamps run backwards are never "older than the newest": past the limit the
            # first kept plays as it is, so the frames waiting here (and the scan above) stay bounded.
            stale = next(iter(self._rx_partial))
            frame, _ = self._rx_partial.pop(stale)
            self.jitter.put(stale & 0xFFFF, bytes(frame))

    def read_frame(self) -> bytes:
        """20 ms of received line audio (PCM16LE 8 kHz) for this tick."""
        self._rx_ticks += 1
        return self.jitter.get()

    def last_media_at(self) -> float:
        return self.rtp.last_received if self.rtp is not None else 0.0

    def write_frame(self, pcm: bytes) -> None:
        """Send this tick's 20 ms of audio, or the DTMF event in progress instead."""
        if self.rtp is None or self.negotiated is None or self.state != "answered":
            return
        if self._dtmf_tick():
            return
        if self.on_hold:
            return
        payload = g711.encode(pcm, self.negotiated.codec)
        self.rtp.send_payload(self.negotiated.payload_type, payload, samples=FRAME_SAMPLES,
                              marker=self._marker_next)
        self._marker_next = False
        self.stats["tx_frames"] += 1

    def _dtmf_tick(self) -> bool:
        """This tick's RFC 4733 packet of the key being sent, if any (True: it goes instead of the audio). Each
        packet takes the telephone-event number of the SDP in force and the next sequence number as it goes out,
        so a re-INVITE or UPDATE that renumbers the event is followed even mid-key, and no number is set aside
        ahead. One that drops telephone-event ends the key in progress (begun, it may already have been heard: it
        is not sent again) and hands the keys still queued to SIP INFO; this tick then carries audio."""
        active = self._dtmf_active
        if active is None and not self._dtmf_queue:
            return False
        event_pt = self.negotiated.dtmf_pt
        if event_pt is None:
            self._dtmf_to_info()
            return False
        if active is None:
            digit = self._dtmf_queue.pop(0)
            start = self.rtp.timestamp
            # Built for their payloads and marker bits only: number and sequence are taken at sending.
            packets = dtmf_packets(digit, payload_type=event_pt, ssrc=self.rtp.ssrc, sequence=0, timestamp=start)
            active = self._dtmf_active = {"packets": packets, "start": start, "ticks": 0, "gap": 0}
            self.stats["dtmf_sent"] += 1
        if active["packets"]:
            packet = active["packets"].pop(0)
            # samples=0: every packet of the event carries its start time; the clock moves on when it ends.
            self.rtp.send_payload(event_pt, packet.payload, samples=0, marker=packet.marker)
            active["ticks"] += 1
            if not active["packets"]:
                self._end_dtmf_event(active)
            return True
        active["gap"] += 1
        if active["gap"] * 20 >= DTMF_GAP_MS:
            self._dtmf_active = None
        return False  # inter-digit gap carries ordinary audio

    def _end_dtmf_event(self, active: dict) -> None:
        """The media clock goes on by the ticks the event took; the audio after it starts a new talkspurt."""
        self.rtp.timestamp = (active["start"] + active["ticks"] * FRAME_SAMPLES) & 0xFFFFFFFF
        self._marker_next = True

    def _dtmf_to_info(self) -> None:
        active, self._dtmf_active = self._dtmf_active, None
        if active is not None and active["packets"]:
            self._end_dtmf_event(active)  # cut short: no end packets under a number the session no longer has
        digits, self._dtmf_queue = "".join(self._dtmf_queue), []
        if digits:
            self.stats["dtmf_by_info"] = self.stats.get("dtmf_by_info", 0) + len(digits)
            self._info_keys = self.ua._spawn(self._queued_keys_by_info(digits, self._info_keys), label="dtmf_info")

    async def _queued_keys_by_info(self, digits: str, previous: Optional[asyncio.Task]) -> None:
        """Keys the RFC 4733 queue held when telephone-event left the session, by SIP INFO in their order, off the
        media clock. A failure ends them (the agent heard "sent" already; later keys alone would be wrong keys)."""
        if previous is not None:
            await asyncio.wait([previous])
        try:
            for digit in digits:
                if self.state != "answered":
                    return
                await self._key_by_info(digit)
        except (SipError, digest.DigestError, SipParseError, ValueError) as exc:
            log.warning("SIP INFO for %d queued keys failed: %s", len(digits), exc)

    async def _key_by_info(self, digit: str) -> None:
        body = f"Signal={digit}\r\nDuration=160\r\n".encode()
        await self.ua._in_dialog_request(self, "INFO", body=body, content_type="application/dtmf-relay")

    async def send_dtmf(self, digits: str) -> str:
        if self.state == "answered" and self.negotiated is not None and self.negotiated.dtmf_pt is None:
            self._dtmf_to_info()  # keys still queued for RFC 4733 move to SIP INFO now, as the next tick would
        pending = self._info_keys
        if pending is not None and not pending.done():
            await asyncio.wait([pending])  # keys moved to SIP INFO by a new SDP go out first
        if self.state != "answered":
            raise SipError("not_answered", "DTMF needs an answered call")
        if self.negotiated is not None and self.negotiated.dtmf_pt is not None:
            self._dtmf_queue.extend(digits)
            return "rfc4733"
        for digit in digits:  # the remote offered no telephone-event: SIP INFO
            await self._key_by_info(digit)
        return "sip_info"

    # -- control ---------------------------------------------------------------------------------
    def _set_state(self, state: str, **data) -> None:
        self.state = state
        self.on_event(self, state, data)

    def _finish(self, reason: str, status: int = 0) -> None:
        if self.ended.done():
            return
        self.end_reason, self.end_status = reason, status
        self.state = "ended"
        for _, _, task in self._pending_ok.values():
            task.cancel()
        self._pending_ok.clear()
        if self.rtp is not None:
            self.rtp.close()
        self.ua._calls.pop(self.id, None)
        if self.direction == "out":
            self.ua._remember_ended(self)
        self.ended.set_result(reason)
        self.on_event(self, "ended", {"reason": reason, "status": status})

    def _bye_once(self, reason: str) -> asyncio.Task:
        """The dialog's one BYE, then the end of the call. Whoever asks next (the agent, a limit, the line
        stopping, a bad answer) waits for the same transaction instead of starting another BYE."""
        if self._bye_task is None:
            self._bye_task = self.ua._track_hangup(
                self.ua._spawn(self._bye_then_finish(reason), call=self, label="bye"))
        return self._bye_task

    async def _bye_then_finish(self, reason: str) -> None:
        try:
            await self.ua._in_dialog_request(self, "BYE")
        except (SipError, digest.DigestError, SipParseError, ValueError):
            pass
        finally:
            self._finish(reason)

    async def hangup(self, reason: str = "local_hangup") -> None:
        """BYE an answered call, CANCEL a ringing outgoing one, decline a ringing incoming one. A BYE already
        under way (a 2xx that raced a CANCEL, an unusable answer) is waited for, whatever the call's state."""
        if self.ended.done():
            return
        if self.state == "answered" or self._bye_task is not None:
            await asyncio.shield(self._bye_once(reason))  # a caller giving up never cancels the BYE itself
        elif self.direction == "out":
            await self.cancel(reason)
        else:
            await self.reject(480, reason=reason)

    async def cancel(self, reason: str = "cancelled") -> None:
        if not self._cancel_wanted:
            self._cancel_wanted = True
            self.end_reason = reason
        tx = self.invite_tx
        if tx is None:
            self._finish(reason)
            return
        if tx.final.done():
            return  # answered as we cancelled: the 2xx gets its ACK and BYE, a failure ends the call
        if tx.provisional_seen:
            self.ua._send_cancel(self)
            return
        # No response yet, so no CANCEL may go out (RFC 3261 §9.1), and the INVITE could run to Timer B (32 s).
        # The call ends here for its owner; its INVITE transaction lives on: a provisional that still comes gets
        # its CANCEL then, and a 2xx its ACK and BYE.
        self._finish(self.end_reason or reason)

    # incoming
    def ringing(self) -> None:
        if self.direction == "in" and self.state == "incoming" and self._incoming_invite is not None:
            self.ua._respond(self._incoming_invite, 180, self._incoming_addr, to_tag=self.dialog.local_tag,
                             extra=[("Contact", self.ua.contact())])
            self.state = "ringing"

    async def accept(self) -> None:
        if self.direction != "in" or self.state not in ("incoming", "ringing"):
            raise SipError("not_ringing", "only a ringing incoming call can be answered")
        invite, addr = self._incoming_invite, self._incoming_addr
        if self.negotiated is not None:
            body = self.local_sdp.answer(self.negotiated).encode()
        else:  # late offer: our 200 carries the offer, the ACK the answer
            body = self.local_sdp.offer(self.ua.config.codecs).encode()
            self._awaiting_ack_answer = True
            self._awaiting_ack_cseq = invite.cseq[0]
        self.ua._send_invite_ok(self, invite, body, addr, initial=True)
        self.reset_media_input()
        self.answered_at = time.monotonic()
        self._set_state("answered")
        if self.on_hold and not self.ended.done():
            self.on_event(self, "hold", {"on_hold": True})  # the caller's offer itself holds the stream

    async def reject(self, status: int = 486, reason: str = "") -> None:
        if self.direction != "in" or self.state not in ("incoming", "ringing"):
            return
        response = response_to(self._incoming_invite, status, to_tag=self.dialog.local_tag,
                               source=self._incoming_addr)
        self.ua._send_invite_failure(self._incoming_invite, response, self._incoming_addr)
        self._finish(reason or f"rejected_{status}", status)


# ---------------------------------------------------------------------------------------------
# the user agent


class SipUA:
    def __init__(self, config: SipConfig, *, on_incoming: Callable[[SipCall], None] = lambda call: None,
                 on_registration: Callable[[str, dict], None] = lambda state, data: None):
        self.config = config
        self.on_incoming = on_incoming
        self.on_registration = on_registration
        self.local_host = config.local_host
        self.local_port = 0
        self.public: Optional[Address] = None
        self.registrar_addr: Optional[Address] = None
        self.proxy_addr: Optional[Address] = None
        self.registration = {"state": "disabled" if not config.register else "starting", "status": 0,
                             "expires": 0, "registered_at": 0.0, "detail": ""}
        self.stats = {"dropped_untrusted": 0, "parse_errors": 0, "keepalives": 0, "sent": 0, "received": 0,
                      "late_2xx_byes": 0, "send_errors": 0}
        self._udp: Optional[_Udp] = None
        self._client_tx: Dict[Tuple[str, str], ClientTransaction] = {}
        # (branch, method) -> (response, address, time, size), oldest first: see SERVER_RESPONSES_MAX
        self._server_responses: Dict[Tuple[str, str], Tuple[SipMessage, Address, float, int]] = {}
        self._server_response_bytes = 0
        self._final_retransmits: Dict[str, asyncio.Task] = {}   # INVITE branch -> Timer G of a non-2xx final
        self._calls: Dict[str, SipCall] = {}
        self._ended: Dict[str, Tuple[SipCall, float]] = {}       # Call-ID -> recently ended outgoing call
        self._tasks: List[asyncio.Task] = []
        self._bg_tasks: Set[asyncio.Task] = set()
        self._hangups: Set[asyncio.Task] = set()   # BYEs still running: a dialog's own, a late or forked 2xx's
        self._stopping = False
        self._stop_task: Optional[asyncio.Task] = None
        self._trusted_ips: Set[str] = set()
        self._trusted_nets: List[ipaddress.IPv4Network] = []
        self._resolved_trust: Dict[str, Set[str]] = {}
        self._reg_call_id = ""
        self._reg_tag = new_tag()
        self._reg_cseq = 0
        self._nc: Dict[str, int] = {}
        self._register_now = asyncio.Event()

    def _spawn(self, coro, *, call: Optional[SipCall] = None, label: str = "") -> asyncio.Task:
        started = False

        async def _guarded() -> None:
            nonlocal started
            started = True
            try:
                await coro
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception("SIP background task %s failed: %s", label or coro, exc)
                if call is not None and not call.ended.done():
                    call._finish("internal_error")

        task = asyncio.ensure_future(_guarded())

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not started:
                coro.close()

        self._bg_tasks.add(task)
        task.add_done_callback(_done)
        return task

    def _track_hangup(self, task: asyncio.Task) -> asyncio.Task:
        """A BYE the line waits for when it stops."""
        self._hangups.add(task)
        task.add_done_callback(self._hangups.discard)
        return task

    # -- lifecycle ---------------------------------------------------------------------------------
    async def _resolve_all(self, host: str, port: int) -> List[str]:
        host = host.strip().strip("[]")
        try:
            return [str(ipaddress.IPv4Address(host))]
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        except (OSError, UnicodeError) as exc:
            raise SipError("dns_failed", f"cannot resolve {host}: {exc}") from None
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses:
            raise SipError("dns_failed", f"cannot resolve {host}")
        return addresses

    async def _resolve(self, host: str, port: int) -> Address:
        return (await self._resolve_all(host, port))[0], port

    async def refresh_trust(self) -> None:
        """Every address of the registrar, the proxy and the trusted sources, read from DNS now."""
        cfg = self.config
        ips: Set[str] = set()
        if self.registrar_addr is not None:
            ips.add(self.registrar_addr[0])
        if self.proxy_addr is not None:
            ips.add(self.proxy_addr[0])
        nets: List[ipaddress.IPv4Network] = []
        hosts = [(cfg.registrar, cfg.registrar_port)]
        if cfg.outbound_proxy:
            host, port = split_hostport(cfg.outbound_proxy)
            hosts.append((host, port))
        for source in cfg.trusted_sources:
            source = source.strip()
            if not source:
                continue
            try:
                network = ipaddress.ip_network(source, strict=False)
            except ValueError:
                hosts.append((source, 5060))
                continue
            if isinstance(network, ipaddress.IPv4Network):
                nets.append(network)
        active_keys: Set[str] = set()
        for host, port in hosts:
            key = host.strip().strip("[]").lower()
            active_keys.add(key)
            try:
                resolved = set(await self._resolve_all(host, port))
                self._resolved_trust[key] = resolved
                ips.update(resolved)
            except SipError as exc:
                log.warning("trusted source not resolved: %s", exc)
                ips.update(self._resolved_trust.get(key, set()))
        for stale in set(self._resolved_trust) - active_keys:
            del self._resolved_trust[stale]
        if ips or nets:
            self._trusted_ips, self._trusted_nets = ips, nets

    async def start(self) -> None:
        if self._stop_task is not None and not self._stop_task.done():
            raise SipError("stopped", "the SIP line is stopping")
        self._stopping = False
        self._stop_task = None  # a line stopped before may start again
        self.registration.update(state="starting" if self.config.register else "disabled", status=0, expires=0,
                                 detail="")
        cfg = self.config
        self.registrar_addr = await self._resolve(cfg.registrar, cfg.registrar_port)
        if cfg.outbound_proxy:
            host, port = split_hostport(cfg.outbound_proxy)
            self.proxy_addr = await self._resolve(host, port)
        if not self.local_host:
            self.local_host = _route_source(self.proxy_addr or self.registrar_addr)
        await self.refresh_trust()
        loop = asyncio.get_running_loop()
        _, udp = await loop.create_datagram_endpoint(lambda: _Udp(self), local_addr=(self.local_host, cfg.local_port))
        if self._stop_task is not None:  # stopped while it started (a reload meeting SIGTERM): nothing may run on
            udp.transport.close()
            raise SipError("stopped", "the SIP line was stopped while it started")
        self._udp = udp
        self.local_port = self._udp.transport.get_extra_info("sockname")[1]
        if cfg.register:
            self._tasks.append(self._spawn(self._registration_loop(), label="registration_loop"))
            self._tasks.append(self._spawn(self._keepalive_loop(), label="keepalive_loop"))
        else:
            self._tasks.append(self._spawn(self._trust_loop(), label="trust_loop"))

    async def stop(self, deadline: Optional[float] = None) -> None:
        """Stop the line by ``deadline`` (``time.monotonic()``; ``STOP_SECONDS`` from now if None). Every call still
        up gets its BYE, CANCEL or 480 at once, and the unregistration goes out with them; all of them, and the
        BYEs already running, are waited for together until the deadline, then whatever is left is given up and the
        socket closes. A second stop waits for the first; after it, a stop returns at once."""
        if self._stop_task is None:
            self._stopping = True
            self._stop_task = asyncio.ensure_future(
                self._stop(time.monotonic() + STOP_SECONDS if deadline is None else deadline))
        await asyncio.shield(self._stop_task)  # a stopper given up never cuts the stop short

    async def _stop(self, deadline: float) -> None:
        loops, self._tasks = list(self._tasks), []
        for task in loops:
            task.cancel()  # registration, keepalive, trust: no refresh may follow the unregistration
        if loops:
            await asyncio.gather(*loops, return_exceptions=True)
        waits: List[asyncio.Future] = list(self._hangups)
        own: List[asyncio.Task] = []
        for call in list(self._calls.values()):
            hangup = asyncio.ensure_future(call.hangup("shutdown"))  # shares a BYE already under way
            hangup.add_done_callback(_consume)
            own.append(hangup)
            waits.append(call.ended)  # a BYE answered, a CANCELled INVITE's final, a 480 sent
        if self.config.register and self.registration["state"] == "registered":
            unregister = asyncio.ensure_future(self._register(expires=0))
            unregister.add_done_callback(_consume)
            own.append(unregister)
            waits.append(unregister)
        pending = [future for future in waits if not future.done()]
        if pending:
            await asyncio.wait(pending, timeout=max(deadline - time.monotonic(), STOP_FLOOR_SECONDS))
        self.registration.update(state="stopped", detail="")
        for tx in list(self._client_tx.values()):
            tx.abandon()
        current = asyncio.current_task()
        tasks = [task for task in dict.fromkeys(own + list(self._final_retransmits.values()) + list(self._bg_tasks))
                 if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._final_retransmits.clear()
        self._bg_tasks.clear()
        self._hangups.clear()
        for call in list(self._calls.values()):
            # A dial still waiting for its answer lost its task above: its media port closes with it.
            call._finish("shutdown")
        udp, self._udp = self._udp, None
        if udp is not None and udp.transport is not None:
            udp.transport.close()

    # -- addressing --------------------------------------------------------------------------------
    def contact_host(self) -> Address:
        return self.public or (self.local_host, self.local_port)

    def sdp_host(self) -> str:
        if self.config.sdp_address == "auto" and self.public is not None:
            return self.public[0]
        if self.config.sdp_address not in ("auto", "local", ""):
            return self.config.sdp_address
        return self.local_host

    def contact(self) -> str:
        host, port = self.contact_host()
        return f"<sip:{self.config.username}@{host}:{port}>"

    def aor(self) -> str:
        return f"sip:{self.config.username}@{self.config.aor_domain}"

    def _from(self, tag: str, user: str = "") -> str:
        display = self.config.display_name.replace('"', "").replace("\\", "")
        name = f'"{display}" ' if display else ""
        return f"{name}<sip:{user or self.config.username}@{self.config.aor_domain}>;tag={tag}"

    def _via(self, branch: str) -> str:
        host, port = self.local_host, self.local_port
        return f"SIP/2.0/UDP {host}:{port};rport;branch={branch}"

    def _out_dest(self) -> Address:
        return self.proxy_addr or self.registrar_addr

    def _trusted_source(self, addr: Address) -> bool:
        if addr[0] in self._trusted_ips:
            return True
        try:
            ip = ipaddress.IPv4Address(addr[0])
        except ValueError:
            return False
        return any(ip in net for net in self._trusted_nets)

    # -- sending ---------------------------------------------------------------------------------------
    @staticmethod
    def _valid_addr(addr: Optional[Address]) -> bool:
        return (isinstance(addr, tuple) and len(addr) == 2
                and isinstance(addr[0], str) and bool(addr[0])
                and type(addr[1]) is int and 1 <= addr[1] <= 65535)

    def _send(self, message: SipMessage, addr: Address) -> None:
        if self._udp is None or self._udp.transport is None:
            return
        if not self._valid_addr(addr):
            self.stats["send_errors"] += 1
            log.warning("SIP send refused bad address %r", addr)
            return
        try:
            self._udp.transport.sendto(message.encode(), addr)
        except (OSError, ValueError, OverflowError) as exc:
            self.stats["send_errors"] += 1
            log.warning("SIP send to %s failed: %s", addr, exc)
            return
        self.stats["sent"] += 1

    def _respond(self, request: SipMessage, status: int, addr: Address, *, to_tag: str = "",
                 extra=None, body: bytes = b"") -> SipMessage:
        response = response_to(request, status, to_tag=to_tag, extra=extra, body=body, source=addr)
        self._remember_response(request, response, addr)
        self._send(response, addr)
        return response

    def _remember_response(self, request: SipMessage, response: SipMessage, addr: Address) -> None:
        key = (request.top_via.branch, request.method)
        previous = self._server_responses.pop(key, None)  # a newer response of the same transaction goes last
        if previous is not None:
            self._server_response_bytes -= previous[3]
        size = len(response.encode())
        self._server_responses[key] = (response, addr, time.monotonic(), size)
        self._server_response_bytes += size
        self._forget_responses()

    def _forget_responses(self) -> None:
        """Drop expired responses, then the oldest beyond the bounds. Only the head of the (time-ordered) table is
        looked at, so each entry costs one removal, whatever a burst of requests does."""
        responses = self._server_responses
        cutoff = time.monotonic() - TRANSACTION_TIMEOUT
        while responses:
            key = next(iter(responses))
            _, _, at, size = responses[key]
            if (at >= cutoff and len(responses) <= SERVER_RESPONSES_MAX
                    and (self._server_response_bytes <= SERVER_RESPONSES_MAX_BYTES or len(responses) == 1)):
                break
            del responses[key]
            self._server_response_bytes -= size
            if at >= cutoff:
                self.stats["responses_forgotten"] = self.stats.get("responses_forgotten", 0) + 1

    def _send_invite_failure(self, invite: SipMessage, response: SipMessage, addr: Address) -> None:
        """A non-2xx final to an INVITE, retransmitted on Timer G until its ACK (Timer H: 64*T1)."""
        self._remember_response(invite, response, addr)
        self._send(response, addr)
        branch = invite.top_via.branch

        async def timer_g() -> None:
            interval, started = T1, time.monotonic()
            try:
                while time.monotonic() - started < TRANSACTION_TIMEOUT:
                    await asyncio.sleep(interval)
                    self._send(response, addr)
                    interval = min(interval * 2, T2)
            except asyncio.CancelledError:
                pass
            finally:
                self._final_retransmits.pop(branch, None)

        previous = self._final_retransmits.pop(branch, None)
        if previous is not None:
            previous.cancel()
        self._final_retransmits[branch] = self._spawn(timer_g(), label="timer_g")

    def _send_invite_ok(self, call: SipCall, invite: SipMessage, body: bytes, addr: Address, *,
                        initial: bool) -> None:
        """A 2xx to an INVITE (initial or re-INVITE), retransmitted until its ACK (§13.3.1.4)."""
        ok = response_to(invite, 200, to_tag=call.dialog.local_tag, body=body, source=addr,
                         extra=[("Contact", self.contact()), ("Allow", ALLOW),
                                ("User-Agent", self.config.user_agent)])
        self._remember_response(invite, ok, addr)
        self._send(ok, addr)
        cseq = invite.cseq[0]

        async def retransmit() -> None:
            interval, started = T1, time.monotonic()
            try:
                while cseq in call._pending_ok:
                    await asyncio.sleep(interval)
                    if cseq not in call._pending_ok:
                        return
                    if time.monotonic() - started >= TRANSACTION_TIMEOUT:
                        call._pending_ok.pop(cseq, None)
                        if initial or call._awaiting_ack_answer and call._awaiting_ack_cseq == cseq:
                            # No ACK in 64*T1: the call, or the offer in this 2xx that its ACK was to answer, is
                            # left unanswered. The session ends with a BYE (RFC 3261 §13.3.1.4, §14.2) rather than
                            # refuse every later INVITE or UPDATE offer while that answer is awaited.
                            await asyncio.shield(call._bye_once("ack_timeout"))
                        return
                    self._send(ok, addr)
                    interval = min(interval * 2, T2)
            except asyncio.CancelledError:
                pass

        call._pending_ok[cseq] = (ok, addr, self._spawn(retransmit(), call=call if initial else None,
                                                        label="ok_retransmit"))

    def _authorize(self, request: SipMessage, challenges: Dict[str, digest.Challenge]) -> bool:
        """Answer every remembered challenge (WWW and proxy) on ``request``, with a fresh nonce count each."""
        if not self.config.password or not challenges:
            return False
        headers: List[Tuple[str, str]] = []
        counts: Dict[str, int] = {}
        try:
            for target, challenge in challenges.items():
                count = counts.get(challenge.nonce, self._nc.get(challenge.nonce, 0)) + 1
                counts[challenge.nonce] = count
                value = digest.authorization(
                    challenge, method=request.method, uri=request.uri,
                    username=self.config.auth_username or self.config.username,
                    password=self.config.password, nc=count, body=request.body)
                headers.append((target, value))
        except digest.DigestError as exc:
            log.warning("SIP auth: %s", exc)
            return False
        self._nc.update(counts)
        for target, value in headers:
            request.set(target, value)
        return True

    @staticmethod
    def _learn_challenge(response: SipMessage, challenges: Dict[str, digest.Challenge]) -> bool:
        header = "WWW-Authenticate" if response.status == 401 else "Proxy-Authenticate"
        target = "Authorization" if response.status == 401 else "Proxy-Authorization"
        try:
            challenges[target] = digest.choose(response.get_all(header))
        except digest.DigestError as exc:
            log.warning("SIP auth: %s", exc)
            return False
        return True

    # -- registration ---------------------------------------------------------------------------------
    def _register_request(self, expires: int) -> SipMessage:
        if not self._reg_call_id:
            self._reg_call_id = new_call_id(self.local_host)
        self._reg_cseq += 1
        domain = self.config.aor_domain
        headers = [("Via", self._via(new_branch())), ("Max-Forwards", "70"),
                   ("From", f"<{self.aor()}>;tag={self._reg_tag}"), ("To", f"<{self.aor()}>"),
                   ("Call-ID", self._reg_call_id), ("CSeq", f"{self._reg_cseq} REGISTER"),
                   ("Contact", self.contact()), ("Expires", str(expires)), ("Allow", ALLOW),
                   ("User-Agent", self.config.user_agent)]
        return SipMessage(method="REGISTER", uri=f"sip:{domain}", headers=headers)

    async def _register(self, expires: int) -> SipMessage:
        challenges: Dict[str, digest.Challenge] = {}
        request = self._register_request(expires)
        response = None
        for attempt in range(4):
            tx = ClientTransaction(self, request, self._out_dest())
            tx.start()
            response = await tx.final
            if response.status in (401, 407) and attempt < 3:
                if not self._learn_challenge(response, challenges):
                    return response
                request = self._register_request(expires)
                try:
                    if not self._authorize(request, challenges):
                        return response
                except digest.DigestError as exc:
                    log.warning("SIP auth: %s", exc)
                    return response
                continue
            if response.status == 423 and attempt < 3:
                minimum = ascii_int(response.get("Min-Expires"))
                if minimum:
                    expires = minimum
                    self.config.expires = expires
                    request = self._register_request(expires)
                    if challenges and not self._authorize(request, challenges):
                        return response
                    continue
            return response
        return response

    def _learn_public(self, response: SipMessage) -> bool:
        via = response.top_via
        received = (via.params.get("received") or "").strip()
        if received:
            try:
                received = str(ipaddress.IPv4Address(received))
            except ValueError:
                received = ""
        rport = ascii_int(via.params.get("rport"))
        if rport is not None and not 1 <= rport <= 65535:
            rport = None
        if not received and not rport:
            return False
        host = received or self.local_host
        port = rport or self.local_port
        public = (host, port)
        if public == (self.local_host, self.local_port):
            changed = self.public is not None
            self.public = None
            return changed
        if public != self.public:
            self.public = public
            return True
        return False

    async def _registration_loop(self) -> None:
        backoff = 15.0
        while True:
            wait = backoff
            try:
                await self.refresh_trust()  # provider addresses may move: re-read DNS at every registration
                response = await self._register(self.config.expires)
                if 200 <= response.status < 300:
                    if self._learn_public(response):
                        continue  # re-register at once with the address the registrar sees
                    granted = self._granted_expires(response)
                    self.registration.update(state="registered", status=response.status, expires=granted,
                                             registered_at=time.time(), detail="")
                    self.on_registration("registered", dict(self.registration))
                    backoff = 15.0
                    wait = max(20.0, granted / 2.0)
                else:
                    self.registration.update(state="failed", status=response.status,
                                             detail=f"{response.status} {response.reason}"[:200])
                    self.on_registration("failed", dict(self.registration))
                    backoff = min(backoff * 2, 300.0)
            except asyncio.CancelledError:
                raise
            except SipError as exc:
                self.registration.update(state="failed", status=exc.status, detail=str(exc)[:200])
                self.on_registration("failed", dict(self.registration))
                backoff = min(backoff * 2, 300.0)
            except Exception as exc:  # a malformed response or challenge never stops the line
                log.warning("registration attempt failed: %s: %s", type(exc).__name__, exc)
                self.registration.update(state="failed", status=0, detail=f"{type(exc).__name__}: {exc}"[:200])
                self.on_registration("failed", dict(self.registration))
                backoff = min(backoff * 2, 300.0)
            self._register_now.clear()
            try:
                await asyncio.wait_for(self._register_now.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def _trust_loop(self) -> None:
        while True:
            await asyncio.sleep(TRUST_REFRESH_SECONDS)
            try:
                await self.refresh_trust()
            except Exception as exc:
                log.warning("trusted sources not refreshed: %s", exc)

    def refresh_registration(self) -> None:
        self._register_now.set()

    def _granted_expires(self, response: SipMessage) -> int:
        mine = self.contact().strip("<>")
        for value in response.get_all("Contact"):
            try:
                contact = NameAddr.parse(value)
            except SipParseError:
                continue
            granted = ascii_int(contact.params.get("expires"))
            if str(contact.uri) == mine and granted is not None:
                return granted
        header = ascii_int(response.get("Expires"))
        return header if header is not None else self.config.expires

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            dest = self._out_dest()
            if self._udp is not None and self._udp.transport is not None and self._valid_addr(dest):
                try:
                    self._udp.transport.sendto(b"\r\n\r\n", dest)  # RFC 5626 §4.4.1 CRLF keepalive
                    self.stats["keepalives"] += 1
                except (OSError, ValueError, OverflowError):
                    self.stats["send_errors"] += 1

    # -- outgoing calls -------------------------------------------------------------------------------
    async def dial(self, number: str, *, on_event: Callable[[SipCall, str, dict], None]) -> SipCall:
        """Start an outgoing call. Returns once the INVITE is sent; events report its progress."""
        if self._stopping:
            raise SipError("stopped", "the SIP line is stopping")
        call = SipCall(self, "out", number)
        call.on_event = on_event
        await call._open_rtp()
        if self._stopping:
            if call.rtp is not None:
                call.rtp.close()
            raise SipError("stopped", "the SIP line is stopping")
        domain = self.config.dial_domain or self.config.aor_domain
        target = f"sip:{number}@{domain}"
        call.dialog = Dialog(new_call_id(self.local_host), new_tag(),
                             local_uri=f"<sip:{self.config.caller_id or self.config.username}@{self.config.aor_domain}>",
                             remote_uri=f"<{target}>")
        call.invite_dest = self._out_dest()
        call.peer_ips.add(call.invite_dest[0])
        self._calls[call.id] = call
        call.invite = self._invite_request(call, target)
        self._spawn(self._run_invite(call), call=call, label="run_invite")
        return call

    def _invite_request(self, call: SipCall, target: str) -> SipMessage:
        dialog = call.dialog
        body = call.local_sdp.offer(self.config.codecs).encode()
        headers = [("Via", self._via(new_branch())), ("Max-Forwards", "70"),
                   ("From", self._from(dialog.local_tag, self.config.caller_id)), ("To", dialog.remote_uri),
                   ("Call-ID", dialog.call_id), ("CSeq", f"{dialog.local_cseq} INVITE"),
                   ("Contact", self.contact()), ("Allow", ALLOW), ("User-Agent", self.config.user_agent),
                   ("Content-Type", "application/sdp")]
        return SipMessage(method="INVITE", uri=target, headers=headers, body=body)

    async def _run_invite(self, call: SipCall) -> None:
        challenges: Dict[str, digest.Challenge] = {}
        attempts = 0
        try:
            while True:
                if call._cancel_wanted or call.ended.done() or self._stopping:
                    call._finish(call.end_reason or "cancelled")
                    return
                tx = ClientTransaction(self, call.invite, call.invite_dest,
                                       on_provisional=lambda r, c=call: self._invite_provisional(c, r))
                call.invite_tx = tx
                call._sent_invites[call.invite.cseq[0]] = call.invite
                tx.start()
                response = await tx.final
                if response.status in (401, 407) and attempts < 2 and not call._cancel_wanted:
                    attempts += 1
                    if not self._learn_challenge(response, challenges):
                        call._finish("auth_failed", response.status)
                        return
                    call.dialog.local_cseq += 1
                    retry = self._invite_request(call, call.invite.uri)
                    if not self._authorize(retry, challenges):
                        call._finish("auth_failed", response.status)
                        return
                    call.invite = retry
                    continue
                break
        except SipError as exc:
            call._finish("no_response" if exc.code in ("timeout", "ringing_too_long") else
                         call.end_reason or exc.code, exc.status)
            return
        except (SipParseError, SdpError, ValueError, digest.DigestError) as exc:
            log.warning("INVITE failed: %s", exc)
            call._finish("bad_response", 0)
            return
        if 200 <= response.status < 300:
            try:
                await self._invite_answered(call, response)
            except (SipParseError, SdpError, ValueError, SipError, digest.DigestError) as exc:
                log.warning("INVITE 2xx handling failed: %s", exc)
                await self._refuse_2xx(call, response, "bad_response")
            return
        reason = {486: "busy", 600: "busy", 603: "declined", 480: "unavailable", 408: "no_answer",
                  487: call.end_reason or "cancelled", 404: "not_found", 484: "address_incomplete",
                  403: "forbidden"}.get(response.status, "failed")
        if call._cancel_wanted and response.status == 487:
            reason = call.end_reason or "cancelled"
        call._finish(reason, response.status)

    def _invite_provisional(self, call: SipCall, response: SipMessage) -> None:
        if response.status == 100:
            if call._cancel_wanted and not call._cancel_sent:
                self._send_cancel(call)
            return
        if call._cancel_wanted:
            if not call._cancel_sent:
                self._send_cancel(call)
            return
        tag = response.to_addr.tag
        if tag and call.dialog is not None and not call.dialog.remote_tag:
            call.dialog.remote_tag = tag
        if response.body and response.get("Content-Type").lower().startswith("application/sdp"):
            try:
                call._apply_remote_sdp(response.body, answer_to_offer=False)
                call.on_event(call, "early_media", {"status": response.status})
            except SdpError:
                pass
        if call.state != "ringing":
            call._set_state("ringing", status=response.status)

    def _send_cancel(self, call: SipCall) -> None:
        """One CANCEL for the current INVITE (§9.1). If even the INVITE's final never comes, the call ends here
        after 64*T1, and the INVITE transaction lingers a while to ACK and BYE a late 2xx."""
        if call._cancel_sent:
            return
        call._cancel_sent = True
        invite = call.invite
        headers = [("Via", invite.get("Via")), ("Max-Forwards", "70"), ("From", invite.get("From")),
                   ("To", invite.get("To")), ("Call-ID", invite.call_id), ("CSeq", f"{invite.cseq[0]} CANCEL"),
                   ("User-Agent", self.config.user_agent)]
        headers += [("Route", r) for r in invite.get_all("Route")]
        cancel = SipMessage(method="CANCEL", uri=invite.uri, headers=headers)
        tx = ClientTransaction(self, cancel, call.invite_dest)
        tx.final.add_done_callback(_consume)
        tx.start()
        invite_tx = call.invite_tx

        def give_up() -> None:
            if invite_tx is not None and not invite_tx.final.done():
                call._finish(call.end_reason or "cancelled", 0)
                # keep the INVITE transaction a while longer: a late 2xx still gets ACK and BYE
                asyncio.get_running_loop().call_later(LATE_2XX_GUARD_SECONDS, invite_tx.abandon)

        asyncio.get_running_loop().call_later(TRANSACTION_TIMEOUT, give_up)

    def _build_2xx_ack(self, call: SipCall, cseq: int) -> SipMessage:
        """The ACK of a 2xx to this call's INVITE numbered ``cseq``: an in-dialog request carrying that INVITE's
        own credentials (``_credentials``), never a digest of its own."""
        ack = self._dialog_request(call, "ACK", cseq=cseq)
        invite = call._sent_invites.get(cseq)
        if invite is not None:
            ack.headers += _credentials(invite)
        return ack

    async def _invite_answered(self, call: SipCall, response: SipMessage) -> None:
        try:
            tag = response.to_addr.tag
            remote_target = _parse_target_uri(response.get("Contact"), call.invite.uri)
            route_set = _parse_route_set(list(reversed(response.get_all("Record-Route"))))
            if not tag:
                raise SipParseError("missing To tag in 2xx")
        except (SipParseError, ValueError) as exc:
            log.warning("unusable 2xx: %s", exc)
            await self._refuse_2xx(call, response, "bad_response")
            return
        dialog = call.dialog
        call._answering_tag = tag
        dialog.remote_tag = tag
        dialog.remote_target = remote_target
        dialog.route_set = route_set
        dest = await self._dialog_dest(call)
        ack = self._build_2xx_ack(call, call.invite.cseq[0])
        call._ack_for_2xx = (ack, dest)
        self._send(ack, dest)
        if call._cancel_wanted or call.ended.done() or self._stopping:
            # The callee answered as we cancelled or gave up: end it properly with a BYE.
            await self._bye_after_race(call)
            return
        if response.body.strip():
            try:
                call._apply_remote_sdp(response.body, answer_to_offer=False)
            except SdpError as exc:
                log.warning("unusable SDP answer: %s", exc)
                await self._bye_after_race(call, reason="bad_sdp")
                return
        elif call.negotiated is None or call.rtp is None or call.rtp.remote is None:
            # No 2xx answer, and no 18x ever gave an address to send media to (an early hold at 0.0.0.0 gives
            # none): the call could only stay silent until the silence limit.
            log.warning("unusable SDP answer: empty body and no media address from early media")
            await self._bye_after_race(call, reason="bad_sdp")
            return
        elif call.on_hold:
            # The 2xx must carry the answer (RFC 3261 §13.2.1) and carries none; the one we have came in an
            # unreliable 18x and held the stream (sendonly, inactive, or 0.0.0.0 after an earlier 18x gave an
            # address), as early media often does. The answered call starts two-way to the last address an 18x
            # gave: if nothing flows, the silence limit ends it with a notice, where an inherited hold would keep
            # a dead call open with no notice at all.
            call.on_hold = False
            call.negotiated = dataclasses.replace(call.negotiated, direction="sendrecv")
        call.reset_media_input()  # early media (ringback, announcements) is not the answering party
        call.answered_at = time.monotonic()
        call._set_state("answered")
        if call.on_hold and not call.ended.done():
            # The 2xx's own answer holds the stream (its SDP applied while the call still rang): news for the agent,
            # and the silence limit is off while it lasts.
            call.on_event(call, "hold", {"on_hold": True})

    async def _refuse_2xx(self, call: SipCall, response: SipMessage, reason: str) -> None:
        """ACK and one BYE for a 2xx we cannot use: every 2xx is ACKed (RFC 3261 §13.2.2.4), and the callee must
        not stay connected to a dead line until its own 2xx timer gives up. When this 2xx's To, Contact or
        Record-Route is what could not be used, the ACK and the BYE go where the INVITE went, to its URI."""
        if call._ack_for_2xx is None:
            try:
                tag = response.to_addr.tag
            except SipParseError:
                tag = ""
            dialog = call.dialog
            dialog.remote_tag = tag
            dialog.remote_target = call.invite.uri
            dialog.route_set = []
            call._answering_tag = call._answering_tag or tag  # its retransmissions get the same ACK again
            call._fixed_dest = call.invite_dest
            ack = self._build_2xx_ack(call, call.invite.cseq[0])
            call._ack_for_2xx = (ack, call.invite_dest)
            self._send(ack, call.invite_dest)
        await self._bye_after_race(call, reason=reason)

    async def _bye_after_race(self, call: SipCall, reason: str = "") -> None:
        tag = call.dialog.remote_tag
        if tag not in call._bye_sent_tags:
            call._bye_sent_tags.add(tag)
            self.stats["late_2xx_byes"] += 1
        await asyncio.shield(call._bye_once(reason or call.end_reason or "cancelled"))

    async def _bye_bad_ack(self, call: SipCall) -> None:
        if call.ended.done():
            return
        await asyncio.shield(call._bye_once("bad_sdp"))

    def _remember_ended(self, call: SipCall) -> None:
        now = time.monotonic()
        self._ended[call.dialog.call_id] = (call, now)
        for key in [k for k, (_, at) in self._ended.items() if now - at > LATE_2XX_GUARD_SECONDS]:
            del self._ended[key]

    # -- in-dialog requests -----------------------------------------------------------------------------
    def _dialog_request(self, call: SipCall, method: str, *, cseq: Optional[int] = None,
                        body: bytes = b"", content_type: str = "") -> SipMessage:
        dialog = call.dialog
        if cseq is None:
            dialog.local_cseq += 1
            cseq = dialog.local_cseq
        local = f"{dialog.local_uri};tag={dialog.local_tag}"
        remote = dialog.remote_uri + (f";tag={dialog.remote_tag}" if dialog.remote_tag else "")
        headers = [("Via", self._via(new_branch())), ("Max-Forwards", "70"), ("From", local), ("To", remote),
                   ("Call-ID", dialog.call_id), ("CSeq", f"{cseq} {method}"), ("User-Agent", self.config.user_agent)]
        uri, routes = dialog.remote_target, list(dialog.route_set)
        if routes and not _loose(routes[0]):
            # A strict router (no ;lr, RFC 3261 §12.2.1.1): it becomes the Request-URI and the remote target
            # closes the route set.
            uri = str(NameAddr.parse(routes[0]).uri)
            routes = routes[1:] + [f"<{dialog.remote_target}>"]
        headers += [("Route", route) for route in routes]
        if method in ("INVITE", "UPDATE"):
            headers.append(("Contact", self.contact()))
        if body:
            headers.append(("Content-Type", content_type or "application/sdp"))
        return SipMessage(method=method, uri=uri, headers=headers, body=body)

    async def _dialog_dest(self, call: SipCall) -> Address:
        """Where in-dialog requests go: the proxy, else the first route, else the remote target, resolved
        without blocking the loop (a Record-Route or Contact may name a host)."""
        if self.proxy_addr is not None:
            return self.proxy_addr
        if call._fixed_dest is not None:
            return call._fixed_dest
        dialog = call.dialog
        uri = NameAddr.parse(dialog.route_set[0]).uri if dialog.route_set else SipUri.parse(dialog.remote_target)
        host, port = uri.address()
        key = (host, port)
        if key not in call._dest_cache:
            try:
                call._dest_cache[key] = await self._resolve(host, port)
            except SipError:
                call._dest_cache[key] = call.invite_dest or self._out_dest()
        dest = call._dest_cache[key]
        call.peer_ips.add(dest[0])
        return dest

    async def _in_dialog_request(self, call: SipCall, method: str, *, body: bytes = b"",
                                 content_type: str = "") -> SipMessage:
        challenges: Dict[str, digest.Challenge] = {}
        request = self._dialog_request(call, method, body=body, content_type=content_type)
        response = None
        for attempt in range(3):
            tx = ClientTransaction(self, request, await self._dialog_dest(call))
            tx.start()
            response = await tx.final
            if response.status in (401, 407) and attempt < 2 and self._learn_challenge(response, challenges):
                retry = self._dialog_request(call, method, body=body, content_type=content_type)
                if self._authorize(retry, challenges):
                    request = retry
                    continue
            return response
        return response

    # -- receiving ------------------------------------------------------------------------------------------
    def _datagram(self, data: bytes, addr: Address) -> None:
        if not data.strip():
            return  # CRLF keepalive
        try:
            message = parse_sip(data)
        except SipParseError as exc:
            self.stats["parse_errors"] += 1
            log.debug("unparseable SIP from %s: %s", addr, exc)
            return
        self.stats["received"] += 1
        try:
            if message.is_request:
                self._request(message, addr)
            else:
                self._response(message, addr)
        except (SipParseError, SdpError, ValueError, digest.DigestError) as exc:
            log.warning("SIP %r from %s failed: %s", message, addr, exc)
            if message.is_request and message.method != "ACK" and self._trusted_source(addr):
                self._send(response_to(message, 400, source=addr), addr)

    def _response(self, response: SipMessage, addr: Address) -> None:
        branch = response.top_via.branch
        _, method = response.cseq
        tx = self._client_tx.get((branch, method))
        if tx is not None:
            # RFC 3261 §17.1.3 matches a response to its transaction by the top Via branch and the CSeq method.
            # The branch is 80 random bits that only the request's own path has seen, so its source address
            # proves nothing more (whoever saw the branch can forge the address too), and checking it would drop
            # a multi-homed server that answers from another interface, leaving our BYE to Timer F.
            call = next((c for c in self._calls.values() if c.invite_tx is tx), None)
            if call is not None:
                call.peer_ips.add(addr[0])
            tx.receive(response)
            return
        if method == "INVITE" and 200 <= response.status < 300:
            self._stray_2xx(response, addr)

    def _stray_2xx(self, response: SipMessage, addr: Address) -> None:
        """A 2xx with no transaction: a retransmission (ACK again) or a late answer to a call that is over
        (ACK and BYE, once). Nothing secret binds it to us (its Call-ID and tag are reused by every message of
        the call), so it is taken only from the trusted addresses and the call's own remote party."""
        call_id = response.call_id
        live = next((c for c in self._calls.values()
                     if c.direction == "out" and c.dialog and c.dialog.call_id == call_id), None)
        ended = self._ended.get(call_id)
        ended_call = ended[0] if ended and ended[0].direction == "out" else None
        call = live or ended_call
        if call is None or call.dialog is None or call.invite is None:
            return
        if not (self._trusted_source(addr) or addr[0] in call.peer_ips):
            self.stats["dropped_untrusted"] += 1
            return
        if response.from_addr.tag != call.dialog.local_tag:
            return
        tag = response.to_addr.tag
        if not tag:
            return
        if tag == call._answering_tag or (call.answered_at is not None and call.dialog.remote_tag == tag):
            # A retransmission for the answering dialog (even while its initial ACK DNS is in flight).
            if call._ack_for_2xx is not None:
                self._send(*call._ack_for_2xx)
            return
        if tag in call._stray_tags or tag in call._bye_sent_tags:
            # Only this dialog's own ACK: while its Contact is still being resolved there is none yet, and the
            # answering dialog's ACK would acknowledge the wrong 2xx.
            cached = call._stray_acks.get(tag)
            if cached is not None:
                self._send(*cached)
            return
        call._stray_tags.add(tag)

        async def ack_and_bye() -> None:
            try:
                stray = SipCall(self, "out", call.remote_number)
                try:
                    remote_target = _parse_target_uri(response.get("Contact"), call.invite.uri)
                    route_set = _parse_route_set(list(reversed(response.get_all("Record-Route"))))
                except (SipParseError, ValueError) as exc:
                    # Its own routing is unusable: the ACK and the BYE go where the INVITE went, to its URI.
                    log.warning("late 2xx with unusable routing: %s", exc)
                    remote_target, route_set = call.invite.uri, []
                    stray._fixed_dest = call.invite_dest
                stray.invite = call.invite
                stray.invite_dest = call.invite_dest
                stray.peer_ips = set(call.peer_ips)
                stray._sent_invites = call._sent_invites
                stray.dialog = Dialog(
                    call.dialog.call_id,
                    call.dialog.local_tag,
                    remote_tag=tag,
                    local_uri=call.dialog.local_uri,
                    remote_uri=call.dialog.remote_uri,
                    remote_target=remote_target,
                    route_set=route_set,
                    local_cseq=max(call.dialog.local_cseq, response.cseq[0]),
                )
                dest = await self._dialog_dest(stray)
                call.peer_ips.update(stray.peer_ips)
                ack = self._build_2xx_ack(stray, response.cseq[0])
                call._stray_acks[tag] = (ack, dest)
                self._send(ack, dest)
                if tag not in call._bye_sent_tags:
                    call._bye_sent_tags.add(tag)
                    self.stats["late_2xx_byes"] += 1
                    try:
                        await self._in_dialog_request(stray, "BYE")
                    except (SipError, digest.DigestError, SipParseError, ValueError):
                        pass
                if not call._answering_tag and not call.ended.done():
                    call._finish(call.end_reason or "cancelled")
            except (SipParseError, SdpError, ValueError, SipError, digest.DigestError) as exc:
                log.warning("stray 2xx cleanup failed: %s", exc)
                if not call._answering_tag and not call.ended.done():
                    call._finish(call.end_reason or "bad_response")

        self._track_hangup(self._spawn(ack_and_bye(), label="stray_2xx"))

    def _find_call(self, request: SipMessage) -> Optional[SipCall]:
        call_id = request.call_id
        to_tag = request.to_addr.tag
        for call in self._calls.values():
            dialog = call.dialog
            if dialog is None or dialog.call_id != call_id:
                continue
            if not to_tag or to_tag == dialog.local_tag:
                return call
        return None

    def _request(self, request: SipMessage, addr: Address) -> None:
        method = request.method
        call = self._find_call(request)
        in_dialog = call is not None and bool(request.to_addr.tag)
        if in_dialog:
            allowed = self._trusted_source(addr) or addr[0] in call.peer_ips
        else:
            allowed = self._trusted_source(addr)
        if not allowed:
            repeat = self._repeat_from(request, addr)
            if repeat is None:
                self.stats["dropped_untrusted"] += 1
                return  # silently: no answer to scanners or strangers
            self._send(repeat, addr)  # its call is over: the same response again, to that same address
            return
        key = (request.top_via.branch, "INVITE" if method == "ACK" else method)
        self._forget_responses()  # expired ones go even when no new response comes
        if method != "ACK" and key in self._server_responses:
            response = self._server_responses[key][0]
            self._send(response, addr)  # retransmitted request
            return
        if method == "ACK":
            timer = self._final_retransmits.pop(request.top_via.branch, None)
            if timer is not None:
                timer.cancel()  # Timer G stops at the ACK of a non-2xx final
            if call is not None:
                cseq_num = request.cseq[0]
                pending = call._pending_ok.pop(cseq_num, None)
                if pending is not None:
                    pending[2].cancel()
                if call._awaiting_ack_answer and call._awaiting_ack_cseq in (None, cseq_num):
                    call._awaiting_ack_answer = False
                    call._awaiting_ack_cseq = None
                    if not request.body.strip():
                        log.warning("late-offer ACK has no SDP answer")
                        self._spawn(self._bye_bad_ack(call), call=call, label="bad_ack_bye")
                    else:
                        try:
                            call._apply_remote_sdp(request.body, answer_to_offer=False)
                        except SdpError as exc:
                            log.warning("unusable SDP answer in ACK: %s", exc)
                            self._spawn(self._bye_bad_ack(call), call=call, label="bad_ack_bye")
            return
        if method == "INVITE" and not request.to_addr.tag:
            if not self._invite_again(request, addr):
                self._incoming_invite(request, addr)
            return
        if method == "CANCEL":
            self._incoming_cancel(request, addr)
            return
        if method == "OPTIONS":
            self._respond(request, 200, addr, extra=[("Allow", ALLOW), ("Accept", "application/sdp")])
            return
        if call is None:
            self._respond(request, 481, addr)
            return
        number, _ = request.cseq
        if call.dialog.remote_cseq and number < call.dialog.remote_cseq:
            self._respond(request, 500, addr)
            return
        call.dialog.remote_cseq = number
        if method == "BYE":
            self._respond(request, 200, addr)
            call._finish("remote_hangup")
        elif method in ("INVITE", "UPDATE"):
            self._reinvite(call, request, addr)
        elif method == "INFO":
            self._respond(request, 200, addr)
            text = request.body.decode("utf-8", "replace")
            for line in text.splitlines():
                name, _, value = line.partition("=")
                digit = value.strip()[:1].upper()
                if name.strip().lower() == "signal" and digit and digit in DTMF_EVENTS:
                    call.stats["dtmf_received"] += 1
                    call.on_event(call, "dtmf", {"digit": digit, "via": "sip_info"})
        elif method == "NOTIFY":
            self._respond(request, 200, addr)
        else:
            self._respond(request, 405, addr, extra=[("Allow", ALLOW)])

    def _repeat_from(self, request: SipMessage, addr: Address) -> Optional[SipMessage]:
        """The response a request from outside the trusted sources still gets: the remembered one of its own earlier
        transmission, if it comes from the very address that response went to (``_same_request``). Anything else
        from there, a malformed request included, is dropped without a word."""
        if request.method == "ACK":
            return None
        try:
            cached = self._server_responses.get((request.top_via.branch, request.method))
            if (cached is None or cached[2] < time.monotonic() - TRANSACTION_TIMEOUT
                    or not _same_request(cached, request, addr)):
                return None
        except SipParseError:
            return None
        return cached[0]

    def _invite_again(self, request: SipMessage, addr: Address) -> bool:
        """An initial INVITE whose response is no longer remembered (the table's bounds): if its branch already
        started a call, or got a final still sent on Timer G, it is a retransmission, answered as RFC 3261 §17.2.1
        asks, and never a second call."""
        branch = request.top_via.branch
        if branch in self._final_retransmits:
            return True  # its final goes out again until the ACK
        for call in self._calls.values():
            first = call._incoming_invite
            if (call.direction != "in" or first is None or first.top_via.branch != branch
                    or first.call_id != request.call_id):
                continue
            if call.state == "ringing":
                self._respond(request, 180, addr, to_tag=call.dialog.local_tag, extra=[("Contact", self.contact())])
            elif call.state == "incoming":
                self._respond(request, 100, addr)
            else:
                pending = call._pending_ok.get(first.cseq[0])
                if pending is not None:
                    self._send(pending[0], pending[1])  # the 2xx, retransmitted until its ACK anyway
            return True
        return False

    def _reinvite(self, call: SipCall, request: SipMessage, addr: Address) -> None:
        contact = request.get("Contact")
        remote_target = _parse_target_uri(contact, call.dialog.remote_target) if contact else call.dialog.remote_target
        if request.method == "UPDATE" and not request.body:
            # No offer: a target refresh only (RFC 3311 §5.2; a session-timer refresh, RFC 4028). Its 200 carries
            # no SDP (an offer may not ride in a response to UPDATE), and an offer of ours whose answer is due in
            # the ACK of a re-INVITE (UDP may deliver this UPDATE first) is still awaited.
            call.dialog.remote_target = remote_target
            self._respond(request, 200, addr, to_tag=call.dialog.local_tag,
                          extra=[("Contact", self.contact()), ("Allow", ALLOW)])
            return
        if request.method == "UPDATE" and call._awaiting_ack_answer:
            # An offer while ours still waits for its answer in that ACK: refused, and the exchange under way, the
            # session and the remote target stay as they are (RFC 3311 §5.2).
            self._respond(request, 491, addr)
            return
        if request.method == "INVITE" and call._awaiting_ack_answer:
            # Another INVITE, with an offer or without, while the ACK carrying the answer to the offer in our 200
            # has not arrived (UDP may bring it later): refused for now, nothing changes, and the ACK is still
            # awaited (RFC 6337 §4.3 UAS-IsI; RFC 3261 §14.2 for the 500 and its Retry-After of 0 to 10 s).
            retry = [("Retry-After", str(secrets.randbelow(11)))]
            self._send_invite_failure(request, response_to(request, 500, extra=retry, source=addr), addr)
            return
        late_offer = not request.body
        if not late_offer:
            try:
                call._apply_remote_sdp(request.body, answer_to_offer=True)
            except SdpError:
                self._respond(request, 488, addr)
                return
        call.local_sdp.bump()
        if late_offer or call.negotiated is None:
            body = call.local_sdp.offer(self.config.codecs).encode()
            call._awaiting_ack_answer = request.method == "INVITE"
            call._awaiting_ack_cseq = request.cseq[0] if request.method == "INVITE" else None
        else:
            body = call.local_sdp.answer(call.negotiated).encode()
        call.dialog.remote_target = remote_target
        if request.method == "INVITE":
            self._send_invite_ok(call, request, body, addr, initial=False)
        else:
            self._respond(request, 200, addr, to_tag=call.dialog.local_tag, body=body,
                          extra=[("Contact", self.contact()), ("Allow", ALLOW)])

    def _incoming_invite(self, request: SipMessage, addr: Address) -> None:
        if self._stopping:
            self._respond(request, 480, addr)
            return
        from_addr = request.from_addr
        local_uri = _name_addr(request.get("To"))
        remote_uri = _name_addr(request.get("From"))
        contact = request.get("Contact")
        remote_target = _parse_target_uri(contact, str(from_addr.uri)) if contact else str(from_addr.uri)
        if not contact:
            SipUri.parse(remote_target).address()
        route_set = _parse_route_set(list(request.get_all("Record-Route")))
        cseq_num = request.cseq[0]
        call = SipCall(self, "in", _caller_number(request))
        call._incoming_invite, call._incoming_addr = request, addr
        call.peer_ips.add(addr[0])
        call.dialog = Dialog(request.call_id, new_tag(), remote_tag=from_addr.tag,
                             local_uri=local_uri, remote_uri=remote_uri,
                             remote_target=remote_target,
                             route_set=route_set, local_cseq=1,
                             remote_cseq=cseq_num)
        self._respond(request, 100, addr)
        call.state = "incoming"
        self._calls[call.id] = call

        async def prepare() -> None:
            try:
                await call._open_rtp()
            except OSError as exc:
                log.warning("incoming call refused: %s", exc)
                await call.reject(503, reason="no_media_port")
                return
            if call.ended.done() or self._stopping:
                if call.rtp is not None:
                    call.rtp.close()  # cancelled while the media port opened: nothing else to do
                if not call.ended.done():
                    await call.reject(480, reason="shutdown")
                return
            try:
                if request.body:
                    call._apply_remote_sdp(request.body, answer_to_offer=True)
            except SdpError as exc:
                log.warning("incoming call refused: %s", exc)
                await call.reject(488, reason="bad_sdp")
                return
            self.on_incoming(call)

        self._spawn(prepare(), call=call, label="incoming_prepare")

    def _incoming_cancel(self, request: SipMessage, addr: Address) -> None:
        branch = request.top_via.branch
        target = next((c for c in self._calls.values() if c.direction == "in" and c._incoming_invite is not None
                       and c._incoming_invite.top_via.branch == branch), None)
        if target is None:
            # A call already declined: its INVITE's server transaction lives on while the final response stands
            # (retransmitted until the ACK). A CANCEL that crossed that response matches it and changes nothing,
            # and is answered 200 (RFC 3261 §9.2); from another address, or for no such INVITE, it is 481.
            declined = self._server_responses.get((branch, "INVITE"))
            matched = (declined is not None and declined[0].status >= 300 and declined[1] == addr
                       and declined[0].call_id == request.call_id and declined[0].cseq[0] == request.cseq[0])
            self._respond(request, 200 if matched else 481, addr)
            return
        self._respond(request, 200, addr)
        if target.state in ("incoming", "ringing"):
            response = response_to(target._incoming_invite, 487, to_tag=target.dialog.local_tag,
                                   source=target._incoming_addr)
            self._send_invite_failure(target._incoming_invite, response, target._incoming_addr)
            target._finish("remote_cancelled", 487)

    def calls(self) -> List[SipCall]:
        return list(self._calls.values())


def _parse_target_uri(header_value: str, fallback: str) -> str:
    if not header_value:
        return fallback
    items = split_commas(header_value)
    if not items:
        return fallback
    uri = NameAddr.parse(items[0]).uri
    uri.address()
    return str(uri)


def _parse_route_set(routes: List[str]) -> List[str]:
    for route in routes:
        NameAddr.parse(route).uri.address()
    return routes


def _same_request(cached: Tuple[SipMessage, Address, float, int], request: SipMessage, addr: Address) -> bool:
    """Whether ``request`` repeats the one whose response ``cached`` holds, from the very address that response went
    to. A far end retransmits a request whose response was lost (RFC 3261 §17.2) even after the call it belonged
    to ended and took that far end's trust with it (a Contact or Record-Route host outside the provider's
    addresses): it gets that same response again, and nothing else."""
    response, sent_to = cached[0], cached[1]
    return sent_to == addr and response.call_id == request.call_id and response.cseq == request.cseq


def _credentials(request: SipMessage) -> List[Tuple[str, str]]:
    """The Authorization and Proxy-Authorization headers of ``request`` exactly as sent, in order. An ACK
    duplicates those of the INVITE it acknowledges (RFC 3261 §22.1, §13.2.2.4): a server accepts for the ACK what
    it accepted for that INVITE and never challenges an ACK, so a digest computed anew for it answers nothing."""
    return [(name, value) for name, value in request.headers
            if name.lower() in ("authorization", "proxy-authorization")]


def _loose(route: str) -> bool:
    try:
        return "lr" in NameAddr.parse(route).uri.params
    except SipParseError:
        return True


def _caller_number(request: SipMessage) -> str:
    """The caller's number from P-Asserted-Identity, Remote-Party-ID or From, sanitised (never free text)."""
    for header in ("P-Asserted-Identity", "Remote-Party-ID", "From"):
        value = request.get(header)
        if not value:
            continue
        try:
            uri = NameAddr.parse(split_commas(value)[0]).uri
        except SipParseError:
            continue
        if uri.user:
            return sanitize_number(uri.user)
        if uri.scheme == "tel" and uri.host:
            return sanitize_number(uri.host)
    return "unknown"


def split_hostport(text: str, default_port: int = 5060) -> Tuple[str, int]:
    """``host[:port]`` as configured (validated by the settings form); a bad port raises ``SipError``."""
    text = text.strip()
    if text.startswith("["):
        end = text.find("]")
        if end <= 1:
            raise SipError("bad_address", f"not a host[:port]: {text!r}")
        host, rest = text[1:end], text[end + 1:]
        if rest and not rest.startswith(":"):
            raise SipError("bad_address", f"not a host[:port]: {text!r}")
        port_text = rest[1:] if rest.startswith(":") else ""
    elif ":" in text:
        if text.count(":") > 1:
            raise SipError("bad_address", f"not a host[:port]: {text!r}")
        host, _, port_text = text.partition(":")
    else:
        host, port_text = text, ""
    port = ascii_int(port_text, default_port) if port_text else default_port
    if port_text and ascii_int(port_text) is None or not 0 < port < 65536 or not host:
        raise SipError("bad_address", f"not a host[:port]: {text!r}")
    return host, port


def _route_source(dest: Address) -> str:
    """The local address the OS would use to reach ``dest`` (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(dest)
        return probe.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        probe.close()


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"
