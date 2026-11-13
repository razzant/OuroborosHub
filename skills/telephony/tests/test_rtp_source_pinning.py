"""Where the line sends its audio when the far end's packets come from another address than its SDP names
(symmetric RTP behind NAT): it follows such a source only while nothing else was followed and the SDP's
address is silent, only a source the call allows, and then keeps it until a new SDP. Real loopback UDP sockets
for the far end and for a second sender; addresses in SDP from TEST-NET are labels, nothing is sent to them."""
import asyncio
import math
import socket
import struct
import time

from phone import g711
from phone.rtp import LATCH_PACKETS, RtpEndpoint, RtpPacket, build_packet, parse_packet
from phone.sip_ua import SipConfig, SipUA
from sipfake import FakeNode

LABEL = ("192.0.2.10", 17000)      # what the far end's SDP names (TEST-NET-1: never reachable)


def run(coro):
    return asyncio.run(coro)


def sdp(address, port):
    return (f"v=0\r\no=- 1 1 IN IP4 {address}\r\ns=-\r\nc=IN IP4 {address}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
            "a=ptime:20\r\na=sendrecv\r\n")


def pcmu(level):
    return g711.encode(struct.pack("<160h", *([level] * 160)), "PCMU")


class Sender:
    """A UDP socket on loopback that sends RTP to the line and keeps what the line sends it."""

    def __init__(self, ssrc, level):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.address = self.sock.getsockname()
        self.ssrc, self.payload, self.seq, self.ts = ssrc, pcmu(level), 1, 1000

    def send(self, port, count=1):
        for _ in range(count):
            self.sock.sendto(build_packet(RtpPacket(0, self.seq, self.ts, self.ssrc, self.payload)),
                             ("127.0.0.1", port))
            self.seq += 1
            self.ts += 160

    def got(self):
        payloads = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except BlockingIOError:
                return payloads
            packet = parse_packet(data)
            if packet is not None:
                payloads.append(packet.payload)

    def close(self):
        self.sock.close()


async def answered_call(node, ua, address, port):
    """An outgoing call whose 200 names ``address:port`` as the far end's media."""
    call = await ua.dial("+15550001001", on_event=lambda *a: None)
    invite, addr = await node.expect("INVITE")
    node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")],
               body=sdp(address, port).encode())
    await node.expect("ACK")
    await asyncio.sleep(0.03)
    assert call.state == "answered"
    return call, invite


async def make_line(node, **kw):
    ua = SipUA(SipConfig(registrar="127.0.0.1", registrar_port=node.port, username="line", register=False,
                         local_host="127.0.0.1", **kw))
    await ua.start()
    return ua


def levels(frames):
    return [struct.unpack("<160h", frame)[0] for frame in frames]


def test_a_followed_nat_source_is_kept_and_a_second_sender_cannot_take_the_stream():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        far, other = Sender(0x1111, 8000), Sender(0x2222, -8000)
        try:
            call, _ = await answered_call(node, ua, *LABEL)
            port = call.rtp.local_port
            far.send(port, LATCH_PACKETS)               # the far end behind NAT: its packets, not its SDP address
            await asyncio.sleep(0.05)
            facts = {"followed": call.rtp.remote == far.address}
            for _ in range(LATCH_PACKETS + 2):          # another sender, its packets between the far end's
                other.send(port)
                await asyncio.sleep(0.01)
                far.send(port)
                await asyncio.sleep(0.01)
            other.send(port, 5)
            await asyncio.sleep(0.05)
            facts["target_after"] = call.rtp.remote == far.address
            heard = levels([call.read_frame() for _ in range(25)])
            facts["other_heard"] = any(level < -4000 for level in heard)
            facts["far_heard"] = any(level > 4000 for level in heard)
            call.write_frame(struct.pack("<160h", *([3000] * 160)))
            await asyncio.sleep(0.05)
            facts["far_got"], facts["other_got"] = len(far.got()), len(other.got())
            facts["foreign"] = call.rtp.stats["foreign"]
            return facts
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()
            far.close()
            other.close()

    facts = run(scenario())
    assert facts["followed"] and facts["target_after"], facts
    assert facts["far_heard"] and not facts["other_heard"], facts
    assert facts["far_got"] == 1 and facts["other_got"] == 0, facts
    assert facts["foreign"] >= LATCH_PACKETS + 7, facts


def test_a_new_ssrc_from_the_followed_address_is_still_heard():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        far = Sender(0x1111, 8000)
        try:
            call, _ = await answered_call(node, ua, *LABEL)
            far.send(call.rtp.local_port, LATCH_PACKETS + 3)
            await asyncio.sleep(0.05)
            first = levels([call.read_frame() for _ in range(10)])
            far.ssrc, far.payload, far.ts = 0x3333, pcmu(5000), 900_000   # a PBX rebuilding its bridge
            far.send(call.rtp.local_port, 8)
            await asyncio.sleep(0.05)
            second = levels([call.read_frame() for _ in range(12)])
            return call.rtp.remote == far.address, first, second, call.stats["rx_resets"]
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()
            far.close()

    kept, first, second, resets = run(scenario())
    assert kept and any(level > 6000 for level in first), first
    assert sum(3000 < level < 6000 for level in second) >= 6 and resets == 1, second


def test_a_new_sdp_address_is_taken_at_once_and_a_new_label_is_learned_again():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        far, moved, relearned = Sender(0x1111, 8000), Sender(0x4444, 6000), Sender(0x5555, 4000)
        try:
            call, invite = await answered_call(node, ua, *LABEL)
            far.send(call.rtp.local_port, LATCH_PACKETS)
            await asyncio.sleep(0.05)
            facts = {"first": call.rtp.remote == far.address}
            uri = f"sip:line@127.0.0.1:{ua.local_port}"
            to_tag = call.dialog.local_tag

            async def reinvite(cseq, address, port):
                request = node.request("INVITE", uri, call_id=invite.call_id, from_tag="remote1", to=f"<{uri}>",
                                       to_tag=to_tag, cseq=cseq, body=sdp(address, port).encode())
                # the far end is the callee of this call: its From is our To, and the reverse
                request.set("From", invite.get("To") + ";tag=remote1")
                request.set("To", invite.get("From"))
                node.send(request, ("127.0.0.1", ua.local_port))
                for _ in range(100):
                    ok = [m for m, _ in node.received if not m.is_request and m.cseq == (cseq, "INVITE")]
                    if ok:
                        break
                    await asyncio.sleep(0.01)
                ack = node.request("ACK", uri, call_id=invite.call_id, from_tag="remote1", to=f"<{uri}>",
                                   to_tag=to_tag, cseq=cseq)
                ack.set("From", invite.get("To") + ";tag=remote1")
                ack.set("To", invite.get("From"))
                node.send(ack, ("127.0.0.1", ua.local_port))
                await asyncio.sleep(0.05)

            await reinvite(2, *LABEL)                    # repeating the SDP must not reopen learning
            moved.send(call.rtp.local_port, LATCH_PACKETS + 2)
            await asyncio.sleep(0.05)
            facts["same_sdp_kept"] = call.rtp.remote == far.address
            await reinvite(3, *moved.address)            # a new address in a new SDP: taken at its first packet
            moved.send(call.rtp.local_port, 1)
            await asyncio.sleep(0.05)
            facts["moved"] = call.rtp.remote == moved.address
            far.send(call.rtp.local_port, LATCH_PACKETS + 2)
            await asyncio.sleep(0.05)
            facts["old_back"] = call.rtp.remote == far.address
            await reinvite(4, "192.0.2.20", 18000)       # a new private label: learning opens again
            relearned.send(call.rtp.local_port, LATCH_PACKETS)
            await asyncio.sleep(0.05)
            facts["relearned"] = call.rtp.remote == relearned.address
            return facts
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()
            for sender in (far, moved, relearned):
                sender.close()

    facts = run(scenario())
    assert facts == {"first": True, "same_sdp_kept": True, "moved": True, "old_back": False,
                     "relearned": True}, facts


class _Capture:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(addr)

    def close(self):
        pass


def packet(ssrc=7):
    return build_packet(RtpPacket(0, 1, 160, ssrc, b"\xff" * 160))


def endpoint(allowed=()):
    seen = []
    ep = RtpEndpoint(seen.append)
    ep.transport = _Capture()
    ep.set_remote(LABEL)
    ep.may_follow = lambda address: address[0] in allowed
    return ep, seen


def test_a_source_the_call_does_not_allow_is_never_followed():
    ep, seen = endpoint(allowed=("203.0.113.5",))
    for _ in range(LATCH_PACKETS * 4):
        ep.datagram_received(packet(), ("198.51.100.77", 5004))
    assert ep.remote == LABEL and seen == []
    for _ in range(LATCH_PACKETS):
        ep.datagram_received(packet(), ("203.0.113.5", 40000))
    assert ep.remote == ("203.0.113.5", 40000) and len(seen) == 1
    for _ in range(LATCH_PACKETS * 4):            # pinned: the same allowed host on another port is not taken
        ep.datagram_received(packet(9), ("203.0.113.5", 40002))
    assert ep.remote == ("203.0.113.5", 40000) and len(seen) == 1


def test_only_an_unbroken_run_of_one_allowed_source_is_followed():
    ep, seen = endpoint(allowed=("203.0.113.5", "203.0.113.6"))
    a, b, stranger = ("203.0.113.5", 40000), ("203.0.113.6", 40000), ("198.51.100.77", 5004)
    for _ in range(LATCH_PACKETS):                  # two allowed sources by turns: neither is followed
        ep.datagram_received(packet(), a)
        ep.datagram_received(packet(), b)
    assert ep.remote == LABEL and seen == []
    for _ in range(LATCH_PACKETS):                  # a stranger in between cannot break an allowed run
        ep.datagram_received(packet(), a)
        ep.datagram_received(packet(), stranger)
    assert ep.remote == a and len(seen) == 1


def test_the_call_allows_its_own_sip_addresses_and_trusted_hosts_only():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node, trusted_sources=("203.0.113.0/28",))
        try:
            facts = {}
            for name, source in (("stranger", ("198.51.100.77", 5004)), ("trusted", ("203.0.113.9", 7000)),
                                 ("dialog_peer", ("198.18.0.7", 7002)), ("registrar", ("127.0.0.1", 7004))):
                call, _ = await answered_call(node, ua, *LABEL)
                if name == "dialog_peer":
                    call.peer_ips.add("198.18.0.7")  # where this call's SIP came from (no trusted host)
                for _ in range(LATCH_PACKETS * 2):
                    call.rtp.datagram_received(packet(), source)
                facts[name] = call.rtp.remote == source
                hangup = asyncio.ensure_future(call.hangup())
                bye, addr = await node.expect("BYE")
                node.reply(bye, addr, 200)
                await hangup
                node.received.clear()
            return facts
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    facts = run(scenario())
    assert facts == {"stranger": False, "trusted": True, "dialog_peer": True, "registrar": True}, facts
