"""What the caller hears across changes of the incoming RTP stream: a clock that must not reset, a new
SSRC, timestamp jumps, a stalled event loop, bursts, late packets of a replaced stream and partial
frames."""
import asyncio
import socket
import time

import pytest

from conftest import run
from phone import g711
from phone import sip_ua
from phone.jitter import Pacer
from phone.rtp import RtpPacket, build_packet
from phone.sdp import Negotiated
from phone.sip_ua import SipConfig, SipUA


def test_rtp_clock_does_not_reset_every_ten_minutes():
    async def scenario():
        ua = SipUA(SipConfig(registrar="127.0.0.1", username="line", register=False))
        call = sip_ua.SipCall(ua, "out", "+15550001001")
        call.negotiated = Negotiated(codec="PCMU", payload_type=0, dtmf_pt=101,
                                     remote=("127.0.0.1", 40000), direction="sendrecv")
        payload = b"\xff" * 160  # PCMU silence
        ts = 10_000
        for seq in range(33_000):  # 11 minutes of 20 ms packets (> 600 s)
            pkt = RtpPacket(payload_type=0, sequence=seq & 0xFFFF, timestamp=ts & 0xFFFFFFFF,
                            ssrc=0x12345678, payload=payload, marker=(seq == 0))
            call._on_rtp(pkt)
            if seq >= 3:
                frame = call.read_frame()
                assert len(frame) == 320
            ts += 160
        assert call.stats["rx_resets"] == 0

        # A single > 600 s jump between consecutive packets DOES reset the clock
        jump_pkt = RtpPacket(payload_type=0, sequence=33_001 & 0xFFFF,
                             timestamp=(ts + 8000 * 601) & 0xFFFFFFFF, ssrc=0x12345678, payload=payload, marker=False)
        call._on_rtp(jump_pkt)
        assert call.stats["rx_resets"] == 1

    run(scenario())


def test_rtp_timestamp_jumps_near_2_31_restart_the_clock_and_keep_the_audio():
    tone = g711.encode(bytes([0x00, 0x20] * 160), "PCMU")  # a steady non-zero level, 20 ms

    async def scenario():
        ua = SipUA(SipConfig(registrar="127.0.0.1", username="line", register=False))
        call = sip_ua.SipCall(ua, "out", "+15550001001")
        call.negotiated = Negotiated(codec="PCMU", payload_type=0, dtmf_pt=101, remote=("127.0.0.1", 40000),
                                     direction="sendrecv")
        state = {"seq": 0, "ts": 1000}

        def feed(count, jump=0):
            state["ts"] = (state["ts"] + jump) & 0xFFFFFFFF
            for _ in range(count):
                call._on_rtp(RtpPacket(0, state["seq"] & 0xFFFF, state["ts"], 0x1234, tone))
                state["seq"] += 1
                state["ts"] = (state["ts"] + 160) & 0xFFFFFFFF

        def heard():
            frames = [call.read_frame() for _ in range(6)]
            return sum(1 for frame in frames if frame.strip(b"\x00"))

        feed(6)
        assert call.stats["rx_resets"] == 0 and heard() >= 3
        resets = 0
        for jump in (2 ** 31, 2 ** 31 - 160, -(2 ** 31 - 160), 8000 * 601):
            feed(6, jump)
            resets += 1
            assert call.stats["rx_resets"] == resets, jump
            assert heard() >= 3, jump  # the audio after the jump is played, not dropped as stale
        feed(6, 8000 * 599)  # a long but real gap (under ten minutes) keeps the clock
        assert call.stats["rx_resets"] == resets and heard() >= 3

    run(scenario())


# -- A new RTP stream mid-call plays on --------------------------------------------------------------------

@pytest.mark.parametrize("ptime", [20, 10])
def test_a_new_ssrc_mid_stream_plays_on_without_silence(ptime):
    tone = g711.encode(bytes([0x00, 0x20] * (ptime * 8)), "PCMU")  # a steady non-zero level
    per_tick = 20 // ptime
    switch = 30 * per_tick + (per_tick - 1)  # 10 ms packets: the new stream starts in the middle of a frame

    async def scenario():
        ua = SipUA(SipConfig(registrar="127.0.0.1", username="line", register=False))
        call = sip_ua.SipCall(ua, "out", "+15550001001")
        call.negotiated = Negotiated(codec="PCMU", payload_type=0, dtmf_pt=101, remote=("127.0.0.1", 40000),
                                     direction="sendrecv")
        stream = {"ssrc": 0x1234, "seq": 100, "ts": 1000}
        frames = []
        packet = 0
        for _tick in range(60):  # the media clock: one 20 ms frame out per tick
            for _ in range(per_tick):
                if packet == switch:  # the PBX rebuilt its bridge: a new SSRC with a clock of its own
                    stream.update(ssrc=0x5678, seq=40000, ts=987654321)
                call._on_rtp(RtpPacket(0, stream["seq"] & 0xFFFF, stream["ts"] & 0xFFFFFFFF, stream["ssrc"], tone))
                stream["seq"] += 1
                stream["ts"] += ptime * 8
                packet += 1
            frames.append(call.read_frame())
        return frames, call.stats["rx_resets"]

    frames, resets = run(scenario())
    started = next(i for i, frame in enumerate(frames) if frame.strip(b"\x00"))
    silent = [i for i, frame in enumerate(frames) if i >= started and not frame.strip(b"\x00")]
    assert silent == [], silent
    assert resets == 1


def test_a_stalled_event_loop_never_drains_the_jitter_buffer_into_silence():
    # The other cause of the stand's broken marker (rx_resets 0, one concealed frame, then silence): after a stall
    # the media clock paid its owed ticks in one burst, before the packets queued meanwhile were read.
    tone = g711.encode(bytes([0x00, 0x20] * 160), "PCMU")

    async def scenario():
        loop = asyncio.get_running_loop()
        ua = SipUA(SipConfig(registrar="127.0.0.1", username="line", register=False, local_host="127.0.0.1"))
        call = sip_ua.SipCall(ua, "out", "+15550001001")
        await call._open_rtp()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.bind(("127.0.0.1", 0))
        sender.setblocking(False)
        source = sender.getsockname()
        call.negotiated = Negotiated(codec="PCMU", payload_type=0, dtmf_pt=101, remote=source, direction="sendrecv")
        call.rtp.set_remote(source)
        target = ("127.0.0.1", call.rtp.local_port)
        sent = {"n": 0}
        started = time.monotonic()

        def send_one():
            n = sent["n"]
            sent["n"] += 1
            sender.sendto(build_packet(RtpPacket(0, n & 0xFFFF, (1000 + 160 * n) & 0xFFFFFFFF, 0x1234, tone)), target)

        async def far_end():  # a packet every 20 ms on its own schedule
            while True:
                await asyncio.sleep(max(0.0, started + sent["n"] * 0.02 - time.monotonic()))
                while started + sent["n"] * 0.02 <= time.monotonic():
                    send_one()

        def stall():  # the loop stalls for 140 ms while the far end goes on sending
            for _ in range(7):
                send_one()
            time.sleep(0.14)

        frames = []

        async def tick():
            frames.append(call.read_frame())

        feeder = asyncio.ensure_future(far_end())
        await asyncio.sleep(0.01)
        pacer = Pacer(tick)
        pacer.start()
        try:
            await asyncio.sleep(0.4)
            loop.call_soon(stall)
            await asyncio.sleep(0.6)
        finally:
            feeder.cancel()
            await pacer.stop()
            await asyncio.gather(feeder, return_exceptions=True)
            call.rtp.close()
            sender.close()
        return frames, dict(call.jitter.stats)

    frames, stats = run(scenario())
    started = next(i for i, frame in enumerate(frames) if frame.strip(b"\x00"))
    silent = [i for i, frame in enumerate(frames) if i >= started and not frame.strip(b"\x00")]
    assert silent == [] and stats["concealed"] == 0, (silent, stats)


# -- Partial frames stay bounded ---------------------------------------------------------------------------

TONE = g711.encode(bytes([0x00, 0x20] * 160), "PCMU")       # 20 ms at a steady non-zero level


SHORT = g711.encode(bytes([0x00, 0x20] * 80), "PCMU")       # 10 ms


def make_call():
    ua = SipUA(SipConfig(registrar="127.0.0.1", username="line", register=False))
    call = sip_ua.SipCall(ua, "out", "+15550001001")
    call.negotiated = Negotiated(codec="PCMU", payload_type=0, dtmf_pt=101, remote=("127.0.0.1", 40000),
                                 direction="sendrecv")
    return call


def test_short_packets_running_backwards_never_pile_up_partial_frames():
    async def scenario():
        call = make_call()
        biggest = 0
        for k in range(3000):  # 10 ms packets, each 20 ms earlier than the last: none ever completes a frame
            call._on_rtp(RtpPacket(0, k & 0xFFFF, (1_000_000 - 160 * k) & 0xFFFFFFFF, 0x1234, SHORT))
            biggest = max(biggest, len(call._rx_partial))
        return biggest, call.jitter.stats["received"]

    biggest, played = run(scenario())
    assert biggest <= 16, biggest
    assert played > 0  # what arrived of the frames pushed out is played, not thrown away


# -- A new RTP stream stays on time ------------------------------------------------------------------------

def lockstep(script, ticks=150):
    """``script(tick, send, a, b)`` sends that tick's packets of streams ``a`` and ``b``; then the media clock
    reads one frame. Returns the call, the frames read and the frames buffered after each read."""

    async def scenario():
        call = make_call()
        a = {"ssrc": 0x1111, "seq": 10, "ts": 5000}
        b = {"ssrc": 0x2222, "seq": 30000, "ts": 77777777}

        def send(stream):
            call._on_rtp(RtpPacket(0, stream["seq"] & 0xFFFF, stream["ts"] & 0xFFFFFFFF, stream["ssrc"], TONE))
            stream["seq"] += 1
            stream["ts"] += 160

        frames, depth = [], []
        for tick in range(ticks):
            script(tick, send, a, b)
            frames.append(call.read_frame())
            depth.append(len(call.jitter))
        return call, frames, depth

    return run(scenario())


def silent_after_start(frames):
    started = next(i for i, frame in enumerate(frames) if frame.strip(b"\x00"))
    return [i for i, frame in enumerate(frames) if i >= started and not frame.strip(b"\x00")]


def test_two_streams_sent_together_for_a_while_leave_no_lasting_delay():
    def script(tick, send, a, b):
        if tick < 40:
            send(a)
        elif tick < 60:  # a PBX sends both for 20 packet times, then only the new one
            send(a)
            send(b)
        else:
            send(b)

    call, frames, depth = lockstep(script)
    assert silent_after_start(frames) == []
    assert depth[-1] <= call.jitter.target and max(depth[60:]) <= call.jitter.target, depth
    assert call.stats["rx_resets"] == 1, call.stats


def test_late_packets_of_the_replaced_stream_add_no_delay():
    def script(tick, send, a, b):
        if tick < 40:
            send(a)
        else:
            send(b)
            if tick in (41, 51, 61):  # three stragglers of the old stream among the new one's packets
                send(a)

    call, frames, depth = lockstep(script)
    assert silent_after_start(frames) == []
    assert depth[-1] == depth[39], (depth[39], depth[-1])
    assert call.stats["rx_resets"] == 1, call.stats


def test_a_buffer_left_deep_by_a_burst_is_trimmed_back_to_its_target():
    held = []

    def script(tick, send, a, b):
        if tick < 20:
            send(a)
        elif tick < 30:  # the network holds ten packets (the buffer runs dry meanwhile)...
            held.append(dict(a))
            a["seq"] += 1
            a["ts"] += 160
        else:
            if tick == 30:  # ...then delivers them at once
                for stream in held:
                    send(stream)
            send(a)

    call, frames, depth = lockstep(script)
    assert max(depth[30:40]) >= 9, depth  # the burst left the buffer deep
    assert depth[-1] <= call.jitter.target and max(depth[-40:]) <= call.jitter.target, depth  # trimmed back
    assert call.jitter.stats.get("trimmed", 0) >= 6, call.jitter.stats


def test_a_replaced_stream_that_comes_back_alone_plays_again_at_once():
    # Guard: dropping the old stream's packets must not silence it when it genuinely comes
    # back, e.g. a short announcement from another SSRC, then the original stream again.
    def script(tick, send, a, b):
        if tick < 40:
            send(a)
        elif tick < 50:
            send(b)
        else:
            if tick == 50:  # its clock ran on meanwhile
                a["seq"] += 10
                a["ts"] += 1600
            send(a)

    call, frames, depth = lockstep(script)
    assert len(silent_after_start(frames)) <= 6, silent_after_start(frames)
    assert all(frame.strip(b"\x00") for frame in frames[-20:])
    assert call.stats["rx_resets"] == 2, call.stats
