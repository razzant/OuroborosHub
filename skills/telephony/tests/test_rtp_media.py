"""Receive path of the line: packet lengths, early media, stream changes, reordering, latching, DTMF."""
import asyncio
import struct

from phone import g711, rtp, sip_ua
from phone.rtp import RtpEndpoint, RtpPacket
from phone.sdp import Negotiated
from phone.sip_ua import SipConfig, SipUA

PCMU_ONE = bytes([0x80])  # decodes to a large positive sample


def make_call():
    ua = SipUA(SipConfig(registrar="127.0.0.1", username="line"))
    call = sip_ua.SipCall(ua, "out", "+15550001001")
    call.negotiated = Negotiated("PCMU", 0, 101, ("198.51.100.20", 40000), "sendrecv")
    return call


def in_loop(fn):
    async def body():
        return fn()
    return asyncio.run(body())


def test_10ms_packets_are_joined_into_20ms_frames():
    def scenario():
        call = make_call()
        for i in range(10):
            call._on_rtp(RtpPacket(0, i, 1000 + i * 80, 1, PCMU_ONE * 80))
        return call.jitter.stats["received"]
    assert in_loop(scenario) == 5  # 100 ms of audio, nothing lost


def test_30ms_packets_are_cut_into_20ms_frames():
    def scenario():
        call = make_call()
        for i in range(10):
            call._on_rtp(RtpPacket(0, i, 1000 + i * 240, 1, PCMU_ONE * 240))
        return call.jitter.stats["received"]
    assert in_loop(scenario) == 15  # 300 ms of audio, nothing lost


def test_a_new_ssrc_starts_its_own_clock():
    def scenario():
        call = make_call()
        for i in range(5):
            call._on_rtp(RtpPacket(0, i, 50_000 + i * 160, 1, PCMU_ONE * 160))
        for _ in range(5):
            call.read_frame()  # playout is running and has used the first stream up
        # the answering party: another SSRC whose clock is 2 s ahead of the first stream's
        for i in range(5):
            call._on_rtp(RtpPacket(0, 100 + i, 50_000 + 100 * 160 + i * 160, 2, PCMU_ONE * 160))
        real = g711.decode(PCMU_ONE * 160, "PCMU")
        heard = [call.read_frame() for _ in range(6)]
        return sum(1 for frame in heard if frame == real)
    # The new stream is heard at once; the old code filled the 2 s jump with concealment and silence.
    assert in_loop(scenario) == 5


def test_a_reordered_first_packet_does_not_reset_the_buffer():
    def scenario():
        call = make_call()
        call._on_rtp(RtpPacket(0, 2, 8160, 1, PCMU_ONE * 160))   # arrives first
        call._on_rtp(RtpPacket(0, 1, 8000, 1, PCMU_ONE * 160))   # the earlier one, late by a packet
        call._on_rtp(RtpPacket(0, 3, 8320, 1, PCMU_ONE * 160))
        return len(call.jitter), call.stats.get("rx_resets", 0)
    buffered, resets = in_loop(scenario)
    assert buffered == 3 and resets == 0


def test_timestamps_across_the_32_bit_wrap_stay_contiguous():
    def scenario():
        call = make_call()
        base = 2**32 - 160 * 5
        for i in range(10):
            call._on_rtp(RtpPacket(0, i, (base + i * 160) & 0xFFFFFFFF, 1, PCMU_ONE * 160))
        frames = [call.read_frame() for _ in range(10)]
        return call.jitter.stats, frames
    stats, frames = in_loop(scenario)
    assert stats["concealed"] == 0 and stats["received"] == 10


def test_early_media_never_reaches_the_conversation_after_the_answer():
    async def scenario():
        from sipfake import SDP_ANSWER, FakeNode
        node = await FakeNode.start()
        ua = SipUA(SipConfig(registrar="127.0.0.1", registrar_port=node.port, username="line", register=False,
                             local_host="127.0.0.1"))
        await ua.start()
        call = await ua.dial("+15550001001", on_event=lambda *a: None)
        invite, addr = await node.expect("INVITE")
        node.reply(invite, addr, 183, body=SDP_ANSWER.format(port=40000).encode())
        await asyncio.sleep(0.1)
        for i in range(8):  # ringback / announcement before the answer
            call._on_rtp(RtpPacket(0, i, 1000 + i * 160, 7, PCMU_ONE * 160))
        assert len(call.jitter) > 0
        node.reply(invite, addr, 200, extra=[("Contact", f"<sip:peer@127.0.0.1:{node.port}>")],
                   body=SDP_ANSWER.format(port=40000).encode())
        await node.expect("ACK")
        await asyncio.sleep(0.05)
        frames = [call.read_frame() for _ in range(5)]
        await ua.stop()
        node.close()
        return len(call.jitter), frames
    left, frames = asyncio.run(scenario())
    assert left == 0 and all(frame == bytes(320) for frame in frames)


class _Transport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(addr)

    def close(self):
        pass


PACKET = bytes([0x80, 0]) + b"\x00\x01" + b"\x00" * 4 + b"\x00\x00\x00\x07" + b"\x55" * 160


def endpoint():
    ep = RtpEndpoint(lambda p: None)
    ep.transport = _Transport()
    ep.set_remote(("198.51.100.20", 40000))
    return ep


def test_one_stray_packet_cannot_capture_the_stream():
    ep = endpoint()
    ep.datagram_received(PACKET, ("203.0.113.66", 5004))
    assert ep.remote == ("198.51.100.20", 40000) and ep.stats["received"] == 0


def test_a_steady_other_source_is_followed_until_the_signalled_one_speaks():
    ep = endpoint()
    for _ in range(rtp.LATCH_PACKETS):  # a gateway behind NAT: its packets come from a mapped port
        ep.datagram_received(PACKET, ("198.51.100.20", 40002))
    assert ep.remote == ("198.51.100.20", 40002)
    ep.datagram_received(PACKET, ("198.51.100.20", 40000))  # the address the SDP named speaks
    assert ep.remote == ("198.51.100.20", 40000)
    ep.datagram_received(PACKET, ("198.51.100.20", 40002))
    assert ep.remote == ("198.51.100.20", 40000)  # and stays the target


def test_a_late_end_packet_of_the_previous_digit_is_not_reported_again():
    receiver = rtp.DtmfReceiver()
    one = rtp.dtmf_packets("1", payload_type=101, ssrc=9, sequence=0, timestamp=1000)
    two = rtp.dtmf_packets("2", payload_type=101, ssrc=9, sequence=20, timestamp=3000)
    seen = [receiver.feed(one[0]), receiver.feed(two[0]), receiver.feed(one[-1]), receiver.feed(two[-1])]
    assert [d for d in seen if d] == ["1", "2"]
