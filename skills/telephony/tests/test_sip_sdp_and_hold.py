"""SDP corner cases and hold: early media, an answer without SDP, a hold in the answer itself or in a
re-INVITE, and a late offer."""
import asyncio

import pytest

from conftest import run, until
from phone.jitter import FRAME_BYTES
from phone.sip_ua import SipUA
from sipfake import FakeNode, SDP, answered, contact, make_ua


def sdp(*, port: int = 40000, direction: str = "sendrecv", address: str = "127.0.0.1", version: int = 1) -> bytes:
    return (f"v=0\r\no=- 1 {version} IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 {address}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
            f"a=ptime:20\r\na={direction}\r\n").encode()


def reinvite(node: FakeNode, ua: SipUA, call, cseq: int, body: bytes):
    request = node.request("INVITE", f"sip:line@127.0.0.1:{ua.local_port}", call_id=call.dialog.call_id,
                           from_tag="remote1", to="<sip:line@127.0.0.1>", to_tag=call.dialog.local_tag,
                           cseq=cseq, body=body)
    node.send(request, ("127.0.0.1", ua.local_port))
    ack = node.request("ACK", f"sip:line@127.0.0.1:{ua.local_port}", call_id=call.dialog.call_id,
                       from_tag="remote1", to="<sip:line@127.0.0.1>", to_tag=call.dialog.local_tag, cseq=cseq)
    return ack


class UdpSink(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.packets.append(data)

    @property
    def port(self):
        return self.transport.get_extra_info("sockname")[1]


# -- 200 OK without SDP after 183 with SDP reuses early-media SDP ------------------------------------------

def test_200_ok_without_sdp_after_183_early_media_keeps_call_alive(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            events = []
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append(k))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 183, to_tag="early1",
                       extra=[("Contact", f"<sip:peer@127.0.0.1:{node.port}>")], body=SDP)
            await asyncio.sleep(0.05)
            assert "early_media" in events and call.negotiated is not None
            node.reply(invite, addr, 200, to_tag="early1",
                       extra=[("Contact", f"<sip:peer@127.0.0.1:{node.port}>")], body=b"")
            await node.expect("ACK")
            await asyncio.sleep(0.05)
            assert call.state == "answered" and not call.ended.done()
            assert node.count("BYE") == 0
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- A 2xx without SDP after held early media --------------------------------------------------------------

@pytest.mark.parametrize("early", ["sendonly", "inactive"])
def test_an_answer_without_sdp_after_held_early_media_starts_two_way(fast, early):
    async def scenario():
        node = await FakeNode.start()
        _, sink = await asyncio.get_running_loop().create_datagram_endpoint(UdpSink, local_addr=("127.0.0.1", 0))
        ua = await make_ua(node)
        try:
            events = []
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append((k, d)))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 183, to_tag="r1", extra=contact(node), body=sdp(port=sink.port, direction=early))
            await asyncio.sleep(0.05)
            assert call.on_hold is True  # early media: the remote only plays ringback
            node.reply(invite, addr, 200, to_tag="r1", extra=contact(node))  # the answer carries no SDP
            await node.expect("ACK")
            await asyncio.sleep(0.05)
            assert call.state == "answered" and not call.ended.done()
            assert call.on_hold is False and call.negotiated.direction == "sendrecv"
            call.write_frame(bytes(FRAME_BYTES))
            for _ in range(20):
                if sink.packets:
                    break
                await asyncio.sleep(0.01)
            assert sink.packets  # the agent is heard
            assert [k for k, _ in events if k == "hold"] == []
        finally:
            await ua.stop()
            sink.transport.close()
            node.close()

    run(scenario())


# -- Early media held at 0.0.0.0, then an answer without SDP -----------------------------------------------

def test_an_answer_without_sdp_after_an_early_hold_at_0_0_0_0_is_hung_up(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        events = []
        try:
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append(k))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 183, to_tag="r1", extra=contact(node), body=sdp(address="0.0.0.0"))
            await asyncio.sleep(0.05)
            held_without_address = call.on_hold and call.rtp.remote is None
            node.reply(invite, addr, 200, to_tag="r1", extra=contact(node))  # the answer carries no SDP
            await node.expect("ACK")
            bye, baddr = await node.expect("BYE")
            node.reply(bye, baddr, 200)
            await asyncio.wait_for(asyncio.shield(call.ended), 2)
            return held_without_address, call.end_reason, events
        finally:
            await ua.stop()
            node.close()

    held_without_address, reason, events = run(scenario())
    assert held_without_address
    assert reason == "bad_sdp" and "answered" not in events and events.count("ended") == 1


def test_an_answer_without_sdp_after_an_earlier_address_still_starts_two_way(fast):
    # A guard, not a regression test: the BYE for an answer without SDP is only for a call that never got
    # any media address.
    async def scenario():
        node = await FakeNode.start()
        loop = asyncio.get_running_loop()
        received = []

        class Sink(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append(data)

        sink, _ = await loop.create_datagram_endpoint(Sink, local_addr=("127.0.0.1", 0))
        port = sink.get_extra_info("sockname")[1]
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 183, to_tag="r1", extra=contact(node), body=sdp(port=port))
            await asyncio.sleep(0.05)
            node.reply(invite, addr, 183, to_tag="r1", extra=contact(node), body=sdp(address="0.0.0.0", version=2))
            await asyncio.sleep(0.05)
            node.reply(invite, addr, 200, to_tag="r1", extra=contact(node))
            await node.expect("ACK")
            await asyncio.sleep(0.05)
            call.write_frame(bytes(320))
            assert await until(lambda: received, 1)
            return call.state, call.on_hold, node.count("BYE")
        finally:
            await ua.stop()
            sink.close()
            node.close()

    assert run(scenario()) == ("answered", False, 0)


# -- Hold via c=IN IP4 0.0.0.0 without direction change emits hold event -----------------------------------

def test_rfc2543_zero_ip_hold_and_resume_emit_hold_events(fast):
    hold_sdp = ("v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
                "m=audio 40000 RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
                "a=ptime:20\r\na=sendrecv\r\n").encode()

    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            events = []
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append((k, d)))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, to_tag="r1",
                       extra=[("Contact", f"<sip:peer@127.0.0.1:{node.port}>")], body=SDP)
            await node.expect("ACK")
            await asyncio.sleep(0.05)

            reinv1 = node.request("INVITE", f"sip:line@127.0.0.1:{ua.local_port}", call_id=call.dialog.call_id,
                                  from_tag="r1", to=f"<sip:line@127.0.0.1>", to_tag=call.dialog.local_tag,
                                  cseq=2, body=hold_sdp)
            node.send(reinv1, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.05)
            ack1 = node.request("ACK", f"sip:line@127.0.0.1:{ua.local_port}", call_id=call.dialog.call_id,
                                from_tag="r1", to=f"<sip:line@127.0.0.1>", to_tag=call.dialog.local_tag, cseq=2)
            node.send(ack1, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.03)
            assert call.on_hold is True
            assert ("hold", {"on_hold": True}) in events

            reinv2 = node.request("INVITE", f"sip:line@127.0.0.1:{ua.local_port}", call_id=call.dialog.call_id,
                                  from_tag="r1", to=f"<sip:line@127.0.0.1>", to_tag=call.dialog.local_tag,
                                  cseq=3, body=SDP)
            node.send(reinv2, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.05)
            ack2 = node.request("ACK", f"sip:line@127.0.0.1:{ua.local_port}", call_id=call.dialog.call_id,
                                from_tag="r1", to=f"<sip:line@127.0.0.1>", to_tag=call.dialog.local_tag, cseq=3)
            node.send(ack2, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.03)
            assert call.on_hold is False
            assert ("hold", {"on_hold": False}) in events
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- Hold events only on a change of hold, and only on an answered call ------------------------------------

def test_no_hold_event_before_the_answer(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            events = []
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append(k))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 183, to_tag="r1", extra=contact(node), body=sdp(direction="sendonly"))
            await asyncio.sleep(0.05)
            node.reply(invite, addr, 200, to_tag="r1", extra=contact(node), body=sdp(direction="sendrecv", version=2))
            await node.expect("ACK")
            await asyncio.sleep(0.05)
            assert call.state == "answered" and call.on_hold is False
            assert "hold" not in events and events.index("answered") >= 0
        finally:
            await ua.stop()
            node.close()

    run(scenario())


def test_a_hold_event_only_when_hold_changes(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            events = []
            call, _, _ = await answered(ua, node, events)
            cseq = 2
            for direction, expected in (("recvonly", []), ("sendonly", [True]), ("sendonly", []),
                                        ("sendrecv", [False])):
                before = len(events)
                ack = reinvite(node, ua, call, cseq, sdp(direction=direction, version=cseq))
                await asyncio.sleep(0.05)
                node.send(ack, ("127.0.0.1", ua.local_port))
                await asyncio.sleep(0.03)
                holds = [d["on_hold"] for k, d in events[before:] if k == "hold"]
                assert holds == expected, (direction, holds)
                cseq += 1
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- A hold that comes with the answer is news -------------------------------------------------------------

@pytest.mark.parametrize("where", ["answer_sdp", "accept"])
def test_a_hold_in_the_answer_itself_is_reported_after_answered(fast, where):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        events = []
        try:
            if where == "answer_sdp":
                call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append((k, d)))
                invite, addr = await node.expect("INVITE")
                node.reply(invite, addr, 183, to_tag="r1", extra=contact(node), body=sdp(direction="sendrecv"))
                await asyncio.sleep(0.05)
                node.reply(invite, addr, 200, to_tag="r1", extra=contact(node),
                           body=sdp(direction="inactive", version=2))
                await node.expect("ACK")
                await asyncio.sleep(0.05)
            else:
                incoming = []
                ua.on_incoming = incoming.append
                request = node.request("INVITE", f"sip:line@127.0.0.1:{ua.local_port}", call_id="in-held",
                                       from_tag="f1", to=f"<sip:line@127.0.0.1:{ua.local_port}>",
                                       body=sdp(direction="sendonly"))
                node.send(request, ("127.0.0.1", ua.local_port))
                assert await until(lambda: incoming, 2)
                call = incoming[0]
                call.on_event = lambda c, k, d: events.append((k, d))
                call.ringing()
                await call.accept()
            return call.state, call.on_hold, events
        finally:
            await ua.stop()
            node.close()

    state, on_hold, events = run(scenario())
    kinds = [k for k, _ in events]
    assert state == "answered" and on_hold is True
    assert "hold" in kinds and kinds.index("hold") > kinds.index("answered"), kinds
    assert [d for k, d in events if k == "hold"] == [{"on_hold": True}]


# -- Late-offer ACK without SDP or with invalid SDP sends BYE bad_sdp --------------------------------------

@pytest.mark.parametrize("ack_body", [
    b"",
    b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 99\r\n",
])
def test_late_offer_ack_without_valid_sdp_sends_bye_immediately(fast, ack_body):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            incoming = []
            ua.on_incoming = incoming.append
            inv = node.request("INVITE", f"sip:line@127.0.0.1:{ua.local_port}", call_id="late-offer-1",
                               from_tag="caller1", to=f"<sip:line@127.0.0.1:{ua.local_port}>", body=b"")
            node.send(inv, ("127.0.0.1", ua.local_port))
            for _ in range(30):
                if incoming:
                    break
                await asyncio.sleep(0.02)
            assert incoming
            call = incoming[0]
            await call.accept()
            await asyncio.sleep(0.03)
            ok = next(m for m, _ in node.received if not m.is_request and m.status == 200)
            ack = node.request("ACK", f"sip:line@127.0.0.1:{ua.local_port}", call_id="late-offer-1",
                               from_tag="caller1", to=f"<sip:line@127.0.0.1:{ua.local_port}>",
                               to_tag=ok.to_addr.tag, cseq=1, body=ack_body)
            node.send(ack, ("127.0.0.1", ua.local_port))
            bye, baddr = await node.expect("BYE", timeout=1.5)
            node.reply(bye, baddr, 200)
            await asyncio.wait_for(asyncio.shield(call.ended), 1.5)
            assert call.end_reason == "bad_sdp"
        finally:
            await ua.stop()
            node.close()

    run(scenario())
