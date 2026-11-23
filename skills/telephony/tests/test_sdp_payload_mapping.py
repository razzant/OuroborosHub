"""Payload type numbers: the line decodes received RTP by the numbers the two SDPs actually gave each format
(RFC 3264 §5.1, §6.1; dynamic numbers by RFC 3551 §3), sends with the numbers the other side asked for, and
takes no packet of a number the SDPs never gave; keys go out under the telephone-event number of the SDP in
force. Real SIP over loopback UDP against a scripted peer, real RTP sockets, the line's own decoder."""
import asyncio
import math
import socket
import struct
import time

import pytest

from phone import g711, rtp, sip_ua
from phone.rtp import RtpPacket, build_packet, parse_packet
from phone.sdp import parse as parse_sdp
from phone.sip_ua import SipConfig, SipUA
from sipfake import FakeNode


def run(coro):
    return asyncio.run(coro)


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


def sdp(port, formats, *, address="127.0.0.1", te=101, channels=""):
    """An SDP body naming ``formats`` [(payload type, "PCMU"/"PCMA")] and telephone-event at ``te``."""
    pts = [pt for pt, _ in formats] + ([te] if te is not None else [])
    lines = ["v=0", f"o=- 1 1 IN IP4 {address}", "s=-", f"c=IN IP4 {address}", "t=0 0",
             f"m=audio {port} RTP/AVP {' '.join(str(pt) for pt in pts)}"]
    lines += [f"a=rtpmap:{pt} {name}/8000{channels}" for pt, name in formats]
    if te is not None:
        lines.append(f"a=rtpmap:{te} telephone-event/8000")
    lines += ["a=ptime:20", "a=sendrecv"]
    return "\r\n".join(lines) + "\r\n"


def tone_payload(codec):
    pcm = struct.pack("<160h", *[int(8000 * math.sin(2 * math.pi * 1000 * i / 8000)) for i in range(160)])
    return g711.encode(pcm, codec)


class Far:
    """The far end's RTP socket on loopback: the port its SDP names, the source of its packets."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.seq, self.ts = 100, 50_000

    def send(self, call, pt, payload, count=10, ssrc=0x1234):
        for _ in range(count):
            self.sock.sendto(build_packet(RtpPacket(pt, self.seq & 0xFFFF, self.ts & 0xFFFFFFFF, ssrc, payload)),
                             ("127.0.0.1", call.rtp.local_port))
            self.seq += 1
            self.ts += 160

    def received(self):
        packets = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except BlockingIOError:
                return packets
            packet = parse_packet(data)
            if packet is not None:
                packets.append(packet)

    def close(self):
        self.sock.close()


async def heard(call, far, pt, payload, count=10, ssrc=0x1234):
    """Frames with sound the line plays after ``count`` packets of ``pt`` from the far end."""
    far.send(call, pt, payload, count, ssrc)
    await asyncio.sleep(0.08)
    frames = [call.read_frame() for _ in range(count + 6)]
    return sum(1 for frame in frames if any(struct.unpack("<160h", frame)))


async def line(node, **kw):
    ua = SipUA(SipConfig(registrar="127.0.0.1", registrar_port=node.port, username="line", register=False,
                         local_host="127.0.0.1", **kw), on_incoming=lambda call: node.incoming.append(call))
    await ua.start()
    return ua


async def incoming_call(node, ua, offer, call_id="pt-in"):
    uri = f"sip:line@127.0.0.1:{ua.local_port}"
    invite = node.request("INVITE", uri, call_id=call_id, from_tag="far", to=f"<{uri}>", body=offer.encode())
    node.send(invite, ("127.0.0.1", ua.local_port))
    assert await until(lambda: node.incoming)
    call = node.incoming[0]
    await call.accept()
    assert await until(lambda: any(not m.is_request and m.status == 200 and m.call_id == call_id
                                   for m, _ in node.received))
    ok = next(m for m, _ in node.received if not m.is_request and m.status == 200 and m.call_id == call_id)
    node.send(node.request("ACK", uri, call_id=call_id, from_tag="far", to=f"<{uri}>", to_tag=ok.to_addr.tag),
              ("127.0.0.1", ua.local_port))
    await asyncio.sleep(0.03)
    return call, ok, uri


async def stop(ua, node, *fars):
    await ua.stop(time.monotonic() + 0.2)
    node.close()
    for far in fars:
        far.close()


@pytest.mark.parametrize("pt,codec", [(96, "PCMU"), (97, "PCMA"), (0, "PCMU"), (8, "PCMA")])
def test_an_incoming_offer_is_heard_under_the_number_it_gave_the_codec(pt, codec):
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        try:
            call, ok, _ = await incoming_call(node, ua, sdp(far.port, [(pt, codec)]))
            answer = parse_sdp(ok.body.decode())
            facts = {"answer_pts": answer.payload_types, "answer_codec": answer.codec_for(pt),
                     "heard": await heard(call, far, pt, tone_payload(codec))}
            other = 0 if pt != 0 else 8  # a number the SDPs never gave this stream
            before = call.stats.get("rx_unknown_pt", 0)
            facts["other_heard"] = await heard(call, far, other, tone_payload("PCMU" if other == 0 else "PCMA"),
                                               ssrc=0x9999)
            facts["other_dropped"] = call.stats.get("rx_unknown_pt", 0) - before
            call.write_frame(struct.pack("<160h", *([3000] * 160)))
            await asyncio.sleep(0.05)
            facts["sent_pts"] = sorted({p.payload_type for p in far.received()})
            return facts
        finally:
            await stop(ua, node, far)

    facts = run(scenario())
    assert facts["answer_pts"] == [pt, 101] and facts["answer_codec"] == codec, facts
    assert facts["heard"] >= 6, facts          # the negotiated number is decoded, dynamic or static
    assert facts["sent_pts"] == [pt], facts      # and is what the line sends with
    assert facts["other_heard"] == 0 and facts["other_dropped"] == 10, facts


def test_an_answer_that_renumbers_is_sent_its_numbers_and_heard_under_both():
    # Our offer lists PCMU 0, PCMA 8, telephone-event 101; the far end answers PCMU as 96 (its numbers may
    # differ from ours, RFC 3264 §6.1 only recommends the same). It receives on 96; it should send on our 0,
    # and an answerer that sends as it receives uses 96: both are PCMU of this stream.
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            offer = parse_sdp(invite.body.decode())
            node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")],
                       body=sdp(far.port, [(96, "PCMU")]).encode())
            await node.expect("ACK")
            await asyncio.sleep(0.03)
            facts = {"offer_pts": offer.payload_types, "send_pt": call.negotiated.payload_type,
                     "heard_ours": await heard(call, far, 0, tone_payload("PCMU")),
                     "heard_theirs": await heard(call, far, 96, tone_payload("PCMU"), ssrc=0x5678)}
            call.write_frame(struct.pack("<160h", *([3000] * 160)))
            await asyncio.sleep(0.05)
            facts["sent_pts"] = sorted({p.payload_type for p in far.received()})
            return facts
        finally:
            await stop(ua, node, far)

    facts = run(scenario())
    assert facts["offer_pts"] == [0, 8, 101] and facts["send_pt"] == 96 and facts["sent_pts"] == [96], facts
    assert facts["heard_ours"] >= 6 and facts["heard_theirs"] >= 6, facts


def test_a_number_the_answer_gives_another_meaning_is_taken_for_neither():
    # The answer gives 101, telephone-event in our offer, to PCMU. Packets of 101 are then neither keys nor
    # sound; PCMU on our own 0 is heard, and the line sends PCMU on 101 as the far end asked.
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        events = []
        try:
            call = await ua.dial("+15550001001", on_event=lambda c, kind, data: events.append(kind))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")],
                       body=sdp(far.port, [(101, "PCMU")], te=None).encode())
            await node.expect("ACK")
            await asyncio.sleep(0.03)
            keys = rtp.dtmf_packets("5", payload_type=101, ssrc=0x4444, sequence=1, timestamp=9000)
            for packet in keys:
                far.sock.sendto(build_packet(packet), ("127.0.0.1", call.rtp.local_port))
            facts = {"heard_101": await heard(call, far, 101, tone_payload("PCMU"), ssrc=0x4444)}
            facts["dtmf"] = events.count("dtmf")
            facts["heard_0"] = await heard(call, far, 0, tone_payload("PCMU"), ssrc=0x5555)
            facts["send_pt"] = call.negotiated.payload_type
            return facts
        finally:
            await stop(ua, node, far)

    facts = run(scenario())
    assert facts["heard_101"] == 0 and facts["dtmf"] == 0 and facts["send_pt"] == 101, facts
    assert facts["heard_0"] >= 6, facts


def test_a_reinvite_that_changes_the_number_and_codec_is_followed():
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        try:
            call, ok, uri = await incoming_call(node, ua, sdp(far.port, [(0, "PCMU")]), call_id="pt-re")
            facts = {"before": await heard(call, far, 0, tone_payload("PCMU"))}
            reinvite = node.request("INVITE", uri, call_id="pt-re", from_tag="far", to=f"<{uri}>",
                                    to_tag=ok.to_addr.tag, cseq=2, body=sdp(far.port, [(97, "PCMA")]).encode())
            node.send(reinvite, ("127.0.0.1", ua.local_port))
            assert await until(lambda: any(not m.is_request and m.status == 200 and m.cseq == (2, "INVITE")
                                           for m, _ in node.received))
            answer = next(m for m, _ in node.received if not m.is_request and m.cseq == (2, "INVITE"))
            node.send(node.request("ACK", uri, call_id="pt-re", from_tag="far", to=f"<{uri}>",
                                   to_tag=ok.to_addr.tag, cseq=2), ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.03)
            media = parse_sdp(answer.body.decode())
            facts["answer"] = (media.payload_types, media.codec_for(97))
            facts["after"] = await heard(call, far, 97, tone_payload("PCMA"), ssrc=0x7777)
            before = call.stats.get("rx_unknown_pt", 0)
            facts["old_number"] = await heard(call, far, 0, tone_payload("PCMU"), ssrc=0x8888)
            facts["old_dropped"] = call.stats.get("rx_unknown_pt", 0) - before
            call.write_frame(struct.pack("<160h", *([3000] * 160)))
            await asyncio.sleep(0.05)
            facts["sent_pts"] = sorted({p.payload_type for p in far.received()})
            return facts
        finally:
            await stop(ua, node, far)

    facts = run(scenario())
    assert facts["before"] >= 6 and facts["answer"] == ([97, 101], "PCMA"), facts
    assert facts["after"] >= 6 and facts["sent_pts"] == [97], facts
    assert facts["old_number"] == 0 and facts["old_dropped"] == 10, facts


def test_keys_on_a_dynamic_telephone_event_number_of_an_offer_are_heard():
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        events = []
        try:
            call, ok, _ = await incoming_call(node, ua, sdp(far.port, [(96, "PCMU")], te=100), call_id="pt-te")
            call.on_event = lambda c, kind, data: events.append((kind, data.get("digit")))
            for packet in rtp.dtmf_packets("7", payload_type=100, ssrc=0x1234, sequence=500, timestamp=70_000):
                far.sock.sendto(build_packet(packet), ("127.0.0.1", call.rtp.local_port))
            await until(lambda: events)
            return parse_sdp(ok.body.decode()).payload_types, list(events)  # before the line stops
        finally:
            await stop(ua, node, far)

    pts, events = run(scenario())
    assert pts == [96, 100] and events == [("dtmf", "7")], (pts, events)


def test_an_answer_that_leaves_no_codec_number_read_alike_is_refused_not_taken_one_way():
    # The answer swaps the static numbers of our offer (0 is PCMA to it, 8 is PCMU): whatever it sends, the line
    # could not tell which codec a packet carries. The 2xx gets its ACK and a BYE instead of a one-way call.
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        events = []
        try:
            call = await ua.dial("+15550001001", on_event=lambda c, kind, data: events.append((kind, dict(data))))
            invite, addr = await node.expect("INVITE")
            swapped = sdp(far.port, [(0, "PCMA"), (8, "PCMU")], te=None)
            node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")],
                       body=swapped.encode())
            await node.expect("ACK")
            bye, bye_addr = await node.expect("BYE")
            node.reply(bye, bye_addr, 200)
            await asyncio.wait_for(asyncio.shield(call.ended), 2)
            return [kind for kind, _ in events], call.end_reason
        finally:
            await stop(ua, node, far)

    kinds, reason = run(scenario())
    assert "answered" not in kinds and reason == "bad_sdp", (kinds, reason)


@pytest.mark.parametrize("codec,ours,other", [("PCMU", 0, 8), ("PCMA", 8, 0)])
def test_an_answer_enables_only_its_accepted_formats_and_decodes_their_samples(codec, ours, other):
    # Offering both codecs does not negotiate both: the answer accepts just one, and no telephone-event.
    async def scenario():
        node = await FakeNode.start()
        ua = await line(node)
        far = Far()
        events = []
        try:
            call = await ua.dial("+15550001001", on_event=lambda c, kind, data: events.append(kind))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")],
                       body=sdp(far.port, [(96, codec)], te=None).encode())
            await node.expect("ACK")
            assert await until(lambda: call.state == "answered")
            payload = tone_payload(codec)
            far.send(call, ours, payload)
            await asyncio.sleep(0.08)
            frames = [call.read_frame() for _ in range(16)]
            facts = {"exact_frames": frames.count(g711.decode(payload, codec))}
            facts["rejected_codec_heard"] = await heard(call, far, other,
                                                        tone_payload("PCMA" if other == 8 else "PCMU"))
            for packet in rtp.dtmf_packets("5", payload_type=101, ssrc=0x4444, sequence=1, timestamp=9000):
                far.sock.sendto(build_packet(packet), ("127.0.0.1", call.rtp.local_port))
            await asyncio.sleep(0.05)
            facts["unnegotiated_keys"] = events.count("dtmf")
            return facts
        finally:
            await stop(ua, node, far)

    facts = run(scenario())
    assert facts["exact_frames"] >= 6, facts  # distinguishes PCMA from PCMU, not merely nonzero sound
    assert facts["rejected_codec_heard"] == 0 and facts["unnegotiated_keys"] == 0, facts


def test_an_answer_cannot_reinterpret_a_payload_number_as_an_unsupported_codec():
    from phone.sdp import negotiate

    remote = parse_sdp(sdp(17000, [(0, "opus"), (96, "PCMU")], te=None))
    agreed = negotiate(remote, offered={0: "PCMU", 8: "PCMA", 101: "telephone-event"})
    assert agreed.receive == {96: "PCMU"}


@pytest.mark.parametrize("mapping", ["101 telephone-event/8000", "100 telephone-event/80000",
                                   "100 telephone-event/8000/2"])
def test_unlisted_or_unsupported_telephone_event_is_not_negotiated(mapping):
    from phone.sdp import negotiate

    # A stray rtpmap is not a media format; a different clock or two channels is not supported either.
    body = sdp(17000, [(96, "PCMU")], te=None).replace("RTP/AVP 96", "RTP/AVP 96 100")
    remote = parse_sdp(body + f"a=rtpmap:{mapping}\r\n")
    agreed = negotiate(remote)
    assert agreed.dtmf_pt is None and agreed.receive == {96: "PCMU"}


# -- A channel count in the rtpmap: one channel is G.711, anything else is not -----------------------------

@pytest.mark.parametrize("codec", ["PCMU", "PCMA"])
@pytest.mark.parametrize("side", ["incoming offer", "answer to our offer"])
def test_a_mono_channel_count_in_the_rtpmap_is_still_g711(side, codec):
    # "PCMU/8000/1" is the codec of "PCMU/8000": the channel count may be left out when it is one (RFC 8866 §6.6).
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        try:
            body = sdp(far.port, [(96, codec)], channels="/1")
            if side == "incoming offer":
                call, ok, _ = await incoming_call(node, ua, body, call_id="mono-in")
                facts = {"answer_pts": parse_sdp(ok.body.decode()).payload_types}
            else:
                call = await ua.dial("+15550001001", on_event=lambda *a: None)
                invite, addr = await node.expect("INVITE")
                node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")],
                           body=body.encode())
                await node.expect("ACK")
                await until(lambda: call.state == "answered")
                facts = {"answer_pts": None}  # our offer was answered: there is no answer of ours to read
            facts.update(state=call.state, send_pt=call.negotiated.payload_type,
                         heard=await heard(call, far, 96, tone_payload(codec)))
            call.write_frame(struct.pack("<160h", *([3000] * 160)))
            await asyncio.sleep(0.05)
            facts.update(sent_pts=sorted({p.payload_type for p in far.received()}), byes=node.count("BYE"),
                         refusals=[m.status for m, _ in node.received if not m.is_request and m.status >= 400])
            return facts
        finally:
            await stop(ua, node, far)

    facts = run(scenario())
    assert facts["state"] == "answered" and facts["byes"] == 0 and facts["refusals"] == [], facts
    assert facts["answer_pts"] == ([96, 101] if side == "incoming offer" else None), facts
    assert facts["send_pt"] == 96 and facts["heard"] >= 6 and facts["sent_pts"] == [96], facts


@pytest.mark.parametrize("rtpmap", ["PCMU/8000/2", "PCMU/16000", "PCMU/16000/1", "PCMU/8000/", "PCMU//1"])
def test_another_clock_rate_or_channel_count_is_not_g711(rtpmap):
    from phone.sdp import SdpError, negotiate

    media = parse_sdp(sdp(17000, [(96, "PCMU")], te=None).replace("PCMU/8000", rtpmap))
    assert media.codec_for(96) is None
    with pytest.raises(SdpError):
        negotiate(media)
    for mono in ("PCMU/8000", "PCMU/8000/1", "pcma/8000/1"):
        assert parse_sdp(sdp(17000, [(96, "PCMU")], te=None).replace("PCMU/8000", mono)).codec_for(96) == \
            mono.split("/")[0].upper()


def test_an_incoming_stereo_offer_is_still_refused_with_488():
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        try:
            uri = f"sip:line@127.0.0.1:{ua.local_port}"
            offer = sdp(17000, [(96, "PCMU")], channels="/2")
            node.send(node.request("INVITE", uri, call_id="stereo", from_tag="far", to=f"<{uri}>",
                                   body=offer.encode()), ("127.0.0.1", ua.local_port))
            await until(lambda: any(not m.is_request and m.status >= 400 for m, _ in node.received))
            return [m.status for m, _ in node.received if not m.is_request], node.incoming
        finally:
            await stop(ua, node)

    statuses, incoming = run(scenario())
    assert 488 in statuses and incoming == [], statuses


# -- Keys queued across an SDP change: the number in force, or SIP INFO once telephone-event is gone --------

LOUD = struct.pack("<160h", *([3000] * 160))


def tick(call, count):
    for _ in range(count):  # the media clock's 20 ms ticks
        call.write_frame(LOUD)


async def reinvite(node, ua, uri, ok, call_id, cseq, body):
    dialog = dict(call_id=call_id, from_tag="far", to=f"<{uri}>", to_tag=ok.to_addr.tag)
    node.send(node.request("INVITE", uri, cseq=cseq, body=body.encode(), **dialog), ("127.0.0.1", ua.local_port))
    assert await until(lambda: any(not m.is_request and m.status == 200 and m.cseq == (cseq, "INVITE")
                                   for m, _ in node.received))
    node.send(node.request("ACK", uri, cseq=cseq, **dialog), ("127.0.0.1", ua.local_port))
    await asyncio.sleep(0.03)


async def answer_infos(node, bodies):
    """The far end answers every INFO transaction at once, noting its body."""
    seen = set()
    while True:
        for message, addr in list(node.received):
            if message.is_request and message.method == "INFO" and message.top_via.branch not in seen:
                seen.add(message.top_via.branch)
                bodies.append(message.body.decode())
                node.reply(message, addr, 200)
        await asyncio.sleep(0.01)


def contiguous(packets):
    return all(b.sequence == (a.sequence + 1) & 0xFFFF for a, b in zip(packets, packets[1:]))


def test_keys_queued_when_a_reinvite_drops_telephone_event_go_by_sip_info_and_the_audio_goes_on():
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        bodies = []
        responder = asyncio.ensure_future(answer_infos(node, bodies))
        try:
            call, ok, uri = await incoming_call(node, ua, sdp(far.port, [(0, "PCMU")]), call_id="te-gone")
            via = await call.send_dtmf("123")
            tick(call, 3)                          # key 1 begins under 101
            await reinvite(node, ua, uri, ok, "te-gone", 2, sdp(far.port, [(0, "PCMU")], te=None))
            tick(call, 10)                         # the next tick already carries audio
            await asyncio.sleep(0.05)
            packets = far.received()
            await until(lambda: len(bodies) == 2)
            await asyncio.sleep(0.1)
            return via, packets, list(bodies), dict(call.stats), call.state
        finally:
            responder.cancel()
            await stop(ua, node, far)

    via, packets, bodies, stats, state = run(scenario())
    keys, audio = packets[:3], packets[3:]
    assert via == "rfc4733" and [p.payload_type for p in keys] == [101] * 3, packets
    assert {rtp.parse_dtmf(p.payload)[0] for p in keys} == {1} and len({p.timestamp for p in keys}) == 1
    assert [p.payload_type for p in audio] == [0] * 10 and contiguous(packets), packets   # no number set aside
    assert audio[0].marker and audio[0].timestamp == (keys[0].timestamp + 3 * 160) & 0xFFFFFFFF
    assert all(b.timestamp == (a.timestamp + 160) & 0xFFFFFFFF for a, b in zip(audio, audio[1:]))
    assert [b.split("\r\n")[0] for b in bodies] == ["Signal=2", "Signal=3"], bodies   # key 1 is not sent again
    assert stats["dtmf_sent"] == 1 and stats["dtmf_by_info"] == 2 and state == "answered", stats


def test_a_reinvite_that_renumbers_telephone_event_mid_key_is_followed_without_a_gap():
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        try:
            call, ok, uri = await incoming_call(node, ua, sdp(far.port, [(0, "PCMU")]), call_id="te-moved")
            await call.send_dtmf("12")
            tick(call, 3)                          # key 1 begins under 101
            await reinvite(node, ua, uri, ok, "te-moved", 2, sdp(far.port, [(0, "PCMU")], te=96))
            tick(call, 7 + 4 + 10 + 2)             # the rest of key 1, the gap, key 2, audio
            await asyncio.sleep(0.05)
            return far.received(), node.count("INFO"), dict(call.stats)
        finally:
            await stop(ua, node, far)

    packets, infos, stats = run(scenario())
    kinds = [p.payload_type for p in packets]
    assert kinds == [101] * 3 + [96] * 7 + [0] * 4 + [96] * 10 + [0] * 2, kinds
    first, second = packets[:10], packets[14:24]
    assert {p.timestamp for p in first} == {first[0].timestamp} and {rtp.parse_dtmf(p.payload)[0] for p in first} == {1}
    assert {rtp.parse_dtmf(p.payload)[0] for p in second} == {2}
    assert second[0].timestamp == (first[0].timestamp + 14 * 160) & 0xFFFFFFFF and second[0].marker
    assert contiguous(packets) and infos == 0 and stats["dtmf_sent"] == 2, (packets, infos, stats)


async def update(node, ua, uri, ok, call_id, cseq, body):
    dialog = dict(call_id=call_id, from_tag="far", to=f"<{uri}>", to_tag=ok.to_addr.tag)
    node.send(node.request("UPDATE", uri, cseq=cseq, body=body.encode(), **dialog), ("127.0.0.1", ua.local_port))
    assert await until(lambda: any(not m.is_request and m.status == 200 and m.cseq == (cseq, "UPDATE")
                                   for m, _ in node.received))


@pytest.mark.parametrize("first", ["media tick", "new keys"])
def test_keys_still_queued_when_telephone_event_goes_leave_by_info_before_later_keys(first):
    # send_dtmf("12") is taken for RFC 4733 and key 1 begins; an UPDATE drops telephone-event; send_dtmf("3") comes
    # after the next 20 ms tick, or before it. Either way key 2 goes by SIP INFO ahead of key 3, key 1 is not sent
    # again, and the audio goes on with unbroken sequence numbers.
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        bodies = []
        responder = asyncio.ensure_future(answer_infos(node, bodies))
        try:
            call, ok, uri = await incoming_call(node, ua, sdp(far.port, [(0, "PCMU")]), call_id="te-order")
            first_via = await call.send_dtmf("12")
            tick(call, 3)                                  # key 1 begins under 101
            await update(node, ua, uri, ok, "te-order", 2, sdp(far.port, [(0, "PCMU")], te=None))
            if first == "media tick":
                tick(call, 1)                              # this tick hands key 2 to SIP INFO
            later_via = await call.send_dtmf("3")          # returns once key 3 is sent
            tick(call, 10)
            await asyncio.sleep(0.05)
            packets = far.received()
            await until(lambda: len(bodies) == 2)
            await asyncio.sleep(0.1)
            return first_via, later_via, packets, list(bodies), dict(call.stats)
        finally:
            responder.cancel()
            await stop(ua, node, far)

    first_via, later_via, packets, bodies, stats = run(scenario())
    keys, audio = packets[:3], packets[3:]
    assert (first_via, later_via) == ("rfc4733", "sip_info"), (first_via, later_via)
    assert [b.split("\r\n")[0] for b in bodies] == ["Signal=2", "Signal=3"], bodies   # key 1 is not sent again
    assert [p.payload_type for p in keys] == [101] * 3 and {rtp.parse_dtmf(p.payload)[0] for p in keys} == {1}
    assert [p.payload_type for p in audio] == [0] * (11 if first == "media tick" else 10), packets
    assert contiguous(packets) and audio[0].marker
    assert audio[0].timestamp == (keys[0].timestamp + 3 * 160) & 0xFFFFFFFF, packets
    assert stats["dtmf_sent"] == 1 and stats["dtmf_by_info"] == 1, stats


def test_keys_sent_after_a_renumbering_join_the_rfc4733_queue_in_order():
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        try:
            call, ok, uri = await incoming_call(node, ua, sdp(far.port, [(0, "PCMU")]), call_id="te-renumber")
            await call.send_dtmf("12")
            tick(call, 3)                                  # key 1 begins under 101
            await update(node, ua, uri, ok, "te-renumber", 2, sdp(far.port, [(0, "PCMU")], te=96))
            via = await call.send_dtmf("3")                # before the next tick
            tick(call, 7 + 4 + 10 + 4 + 10 + 2)            # the rest of key 1, a gap, key 2, a gap, key 3, audio
            await asyncio.sleep(0.05)
            return via, far.received(), node.count("INFO")
        finally:
            await stop(ua, node, far)

    via, packets, infos = run(scenario())
    kinds = [p.payload_type for p in packets]
    assert via == "rfc4733" and infos == 0, (via, infos)
    assert kinds == [101] * 3 + [96] * 7 + [0] * 4 + [96] * 10 + [0] * 4 + [96] * 10 + [0] * 2, kinds
    events = [{rtp.parse_dtmf(p.payload)[0] for p in packets[a:b]} for a, b in ((0, 10), (14, 24), (28, 38))]
    assert events == [{1}, {2}, {3}] and contiguous(packets), events


# -- An INVITE that arrives before the ACK carrying the answer to the offer in our 200 ----------------------

@pytest.mark.parametrize("new_offer", [True, False], ids=["with an offer", "without an offer"])
@pytest.mark.parametrize("pending", ["initial INVITE", "re-INVITE"])
def test_an_invite_before_the_ack_with_the_awaited_answer_gets_500_and_the_late_ack_still_counts(pending, new_offer):
    # Our 200 to a late-offer INVITE (the initial one or a re-INVITE) carries the offer; its ACK carries the answer.
    # Another INVITE that UDP brings before that ACK, with an offer or without, gets 500 with a Retry-After of 0 to
    # 10 s and changes nothing (RFC 6337 §4.3, RFC 3261 §14.2): no new offer of ours, the session as it was. The
    # late ACK's answer is still taken, and the INVITE sent again after it works as any re-INVITE: PCMA on its new
    # address is heard and sent.
    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        old, mid, new = Far(), Far(), Far()
        uri = f"sip:line@127.0.0.1:{ua.local_port}"

        async def final(cseq):
            assert await until(lambda: any(not m.is_request and m.status >= 200 and m.cseq == (cseq, "INVITE")
                                           for m, _ in node.received))
            return next(m for m, _ in node.received if not m.is_request and m.status >= 200
                        and m.cseq == (cseq, "INVITE"))

        try:
            if pending == "initial INVITE":
                node.send(node.request("INVITE", uri, call_id="glare", from_tag="far", to=f"<{uri}>"),
                          ("127.0.0.1", ua.local_port))  # no SDP: our 200 makes the offer
                assert await until(lambda: node.incoming)
                call, cseq = node.incoming[0], 1
                await call.accept()
            else:
                call, ok, _ = await incoming_call(node, ua, sdp(old.port, [(0, "PCMU")]), call_id="glare")
                cseq = 2
                node.send(node.request("INVITE", uri, call_id="glare", from_tag="far", to=f"<{uri}>",
                                       to_tag=ok.to_addr.tag, cseq=cseq), ("127.0.0.1", ua.local_port))
            ours = await final(cseq)
            dialog = dict(call_id="glare", from_tag="far", to=f"<{uri}>", to_tag=ours.to_addr.tag)
            before = (call.negotiated, call.local_sdp.version, call.dialog.remote_target)
            body = sdp(new.port, [(8, "PCMA")]).encode() if new_offer else b""
            second = node.request("INVITE", uri, cseq=cseq + 1, body=body, **dialog)
            node.send(second, ("127.0.0.1", ua.local_port))
            refused = await final(cseq + 1)
            during = (call.negotiated, call.local_sdp.version, call.dialog.remote_target)
            node.send(node.request("ACK", uri, cseq=cseq + 1, branch=second.top_via.branch, **dialog),
                      ("127.0.0.1", ua.local_port))               # the ACK of the 500
            node.send(node.request("ACK", uri, cseq=cseq, body=sdp(mid.port, [(0, "PCMU")]).encode(), **dialog),
                      ("127.0.0.1", ua.local_port))               # the late ACK with the awaited answer
            await asyncio.sleep(0.05)
            agreed = call.negotiated   # None if the late ACK's answer was lost
            after_ack = (agreed and agreed.codec, agreed and agreed.remote,
                         second.top_via.branch in ua._final_retransmits)
            node.send(node.request("INVITE", uri, cseq=cseq + 2, body=body, **dialog), ("127.0.0.1", ua.local_port))
            again = await final(cseq + 2)
            answer = sdp(new.port, [(8, "PCMA")]).encode() if not new_offer else b""
            node.send(node.request("ACK", uri, cseq=cseq + 2, body=answer, **dialog), ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.05)
            facts = {"ours": b"m=audio" in ours.body, "refused": (refused.status, refused.body),
                     "retry_after": refused.get("Retry-After"), "unchanged": during == before,
                     "after_ack": after_ack, "mid": ("PCMU", ("127.0.0.1", mid.port), False),
                     "again": (again.status, b"m=audio" in again.body),
                     "now": (call.negotiated.codec, call.negotiated.remote), "new": ("PCMA", ("127.0.0.1", new.port)),
                     "heard": await heard(call, new, 8, tone_payload("PCMA")), "state": call.state}
            call.write_frame(LOUD)
            await asyncio.sleep(0.05)
            facts["sent_pts"] = sorted({p.payload_type for p in new.received()})
            return facts
        finally:
            await stop(ua, node, old, mid, new)

    facts = run(scenario())
    assert facts["ours"] and facts["refused"] == (500, b"") and 0 <= int(facts["retry_after"]) <= 10, facts
    assert facts["unchanged"], facts                      # no new offer, no session change, no target change
    assert facts["after_ack"] == facts["mid"], facts      # the late ACK's answer is taken; the 500's ACK ends it
    assert facts["again"] == (200, True) and facts["now"] == facts["new"], facts
    assert facts["heard"] >= 6 and facts["sent_pts"] == [8] and facts["state"] == "answered", facts


# -- The ACK that was to carry the answer to the offer in our 200 never comes ------------------------------

async def final_response(node, cseq, method="INVITE"):
    assert await until(lambda: any(not m.is_request and m.status >= 200 and m.cseq == (cseq, method)
                                   for m, _ in node.received))
    return next(m for m, _ in node.received if not m.is_request and m.status >= 200 and m.cseq == (cseq, method))


@pytest.mark.parametrize("ack", ["never", "in time", "only for a newer offer"])
def test_an_offer_in_our_200_whose_ack_never_comes_ends_the_call_and_nothing_else_does(fast, monkeypatch, ack):
    # Our 200 to a late-offer re-INVITE carries the offer, and its ACK the answer, retransmitted for and awaited
    # 64*T1. If that ACK never comes, an offer nobody answered is left in the session: the line ends the call with
    # a BYE (RFC 3261 §14.2) instead of refusing every later INVITE until the call's own limit. An ACK in time keeps
    # the call and later INVITEs work. The timer of an older 2xx (a re-INVITE that carried its own offer, its ACK
    # lost) running out ends nothing while a newer offer of ours still waits for its answer.
    monkeypatch.setattr(sip_ua, "TRANSACTION_TIMEOUT", 1.2)

    async def scenario():
        node = await FakeNode.start()
        node.incoming = []
        ua = await line(node)
        far = Far()
        try:
            call, ok, uri = await incoming_call(node, ua, sdp(far.port, [(0, "PCMU")]), call_id="ack-expiry")
            dialog = dict(call_id="ack-expiry", from_tag="far", to=f"<{uri}>", to_tag=ok.to_addr.tag)
            to_line = ("127.0.0.1", ua.local_port)
            late = 2
            if ack == "only for a newer offer":
                node.send(node.request("INVITE", uri, cseq=2, body=sdp(far.port, [(0, "PCMU")]).encode(), **dialog),
                          to_line)                               # its own offer; its 2xx gets no ACK
                await final_response(node, 2)
                await asyncio.sleep(0.6)
                late = 3
            node.send(node.request("INVITE", uri, cseq=late, **dialog), to_line)   # no SDP: our 200 offers
            ours = await final_response(node, late)
            answer = sdp(far.port, [(0, "PCMU")]).encode()
            if ack == "in time":
                node.send(node.request("ACK", uri, cseq=late, body=answer, **dialog), to_line)
            elif ack == "only for a newer offer":
                await asyncio.sleep(1.0)                         # CSeq 2's timer runs out while CSeq 3 awaits
                node.send(node.request("ACK", uri, cseq=late, body=answer, **dialog), to_line)
            await asyncio.sleep(sip_ua.TRANSACTION_TIMEOUT + 0.6)   # past every 2xx timer
            byes = [(m, a) for m, a in node.received if m.is_request and m.method == "BYE"]
            for bye, addr in byes:
                node.reply(bye, addr, 200)
            await asyncio.sleep(0.1)
            facts = {"offer": b"m=audio" in ours.body, "byes": len({m.top_via.branch for m, _ in byes}),
                     "ended": call.ended.done(), "reason": call.end_reason}
            if not call.ended.done():
                node.send(node.request("INVITE", uri, cseq=late + 1, body=sdp(far.port, [(8, "PCMA")]).encode(),
                                       **dialog), to_line)
                again = await final_response(node, late + 1)
                node.send(node.request("ACK", uri, cseq=late + 1, **dialog), to_line)
                await asyncio.sleep(0.05)
                facts.update(again=again.status, now=(call.negotiated.codec, call.negotiated.remote),
                             want=("PCMA", ("127.0.0.1", far.port)))
            return facts
        finally:
            await stop(ua, node, far)

    facts = run(scenario())
    assert facts["offer"], facts
    if ack == "never":
        assert facts["byes"] == 1 and facts["ended"] and facts["reason"] == "ack_timeout", facts
    else:
        assert facts["byes"] == 0 and not facts["ended"], facts
        assert facts["again"] == 200 and facts["now"] == facts["want"], facts
