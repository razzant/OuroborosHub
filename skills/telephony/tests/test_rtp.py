import struct

from phone import rtp


def test_build_parse_roundtrip_with_extension_and_padding():
    pkt = rtp.RtpPacket(0, 65535, 4294967295, 0x11223344, b"\xff" * 160, marker=True)
    data = rtp.build_packet(pkt)
    assert rtp.parse_packet(data) == pkt
    # one CSRC, an extension and two padding bytes
    first = 0x80 | 0x20 | 0x10 | 1
    raw = struct.pack("!BBHII", first, 8, 1, 2, 3) + b"CSRC" + struct.pack("!HH", 0xBEDE, 1) + b"EXT!" + b"ab" + b"\x00\x02"
    parsed = rtp.parse_packet(raw)
    assert parsed.payload == b"ab" and parsed.payload_type == 8
    assert rtp.parse_packet(b"\x81\xc8" + bytes(10)) is None  # RTCP sender report


def test_dtmf_packets_follow_rfc4733():
    packets = rtp.dtmf_packets("5", payload_type=101, ssrc=1, sequence=100, timestamp=8000)
    assert packets[0].marker and not any(p.marker for p in packets[1:])
    assert {p.timestamp for p in packets} == {8000}
    assert [p.sequence for p in packets] == list(range(100, 100 + len(packets)))
    parsed = [rtp.parse_dtmf(p.payload) for p in packets]
    assert all(event == 5 for event, *_ in parsed)
    durations = [d for _, end, _, d in parsed if not end]
    assert durations == sorted(durations) and durations[0] == 160
    ends = [p for p in parsed if p[1]]
    assert len(ends) == 3 and all(p[3] == 1280 for p in ends)


def test_dtmf_receiver_reports_each_digit_once():
    rx = rtp.DtmfReceiver()
    seen = []
    for digit, ts in (("1", 100), ("#", 2000), ("1", 4000)):
        for p in rtp.dtmf_packets(digit, payload_type=101, ssrc=1, sequence=0, timestamp=ts):
            got = rx.feed(p)
            if got:
                seen.append(got)
    assert seen == ["1", "#", "1"]
    assert rtp.dtmf_event_code("*") == 10 and rtp.dtmf_event_code("d") == 15
