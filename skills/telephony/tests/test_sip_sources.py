"""Whom the SIP user agent listens to: trusted sources and their DNS, stray or forged responses, and
values from the network (ports, rport, received) that must never take a socket down."""
import asyncio
import gc
import socket

import pytest

from conftest import run
from phone.rtp import RtpEndpoint
from phone.sdp import SdpError, parse as parse_sdp
from phone.sip_message import SipMessage, SipParseError, SipUri, response_to
from phone.sip_ua import SipConfig, SipUA
from sipfake import FakeNode, SDP, SDP_ANSWER, answered, make_ua


# -- refresh_trust preserves registrar IPs on DNS error and includes active endpoints ----------------------

def test_refresh_trust_keeps_previous_ips_and_endpoints_on_dns_error(fast, monkeypatch):
    state = {"mode": "initial"}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "pbx-trust.test":
            if state["mode"] == "initial":
                return [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", port or 0)),
                        (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("192.0.2.44", port or 0))]
            if state["mode"] == "fail":
                raise socket.gaierror("temporary DNS failure")
            if state["mode"] == "rotated":
                return [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("198.51.100.7", port or 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node, registrar="pbx-trust.test", trusted_sources=("10.0.0.0/8",))
        try:
            assert ua._trusted_source(("192.0.2.44", 5060))
            state["mode"] = "fail"
            await ua.refresh_trust()
            # Transient DNS failure must NOT wipe previously resolved registrar IPs
            assert ua._trusted_source(("192.0.2.44", 5060))
            assert ua._trusted_source(("127.0.0.1", node.port))

            state["mode"] = "rotated"
            await ua.refresh_trust()
            # Active registrar_addr stays trusted even if DNS rotated to a new address
            assert ua._trusted_source(("198.51.100.7", 5060))
            assert ua._trusted_source(("127.0.0.1", node.port))
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- _stray_2xx source & direction checks, exception safety, ascii_int port --------------------------------

def test_stray_2xx_checks_direction_and_source_and_handles_bad_contact(fast):
    errors = []

    with pytest.raises(SipParseError):
        SipUri("sip", "peer", "127.0.0.1", params={"port": "²"}).address()

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
        node = await FakeNode.start()
        attacker = await FakeNode.start()
        victim = await FakeNode.start()
        ua = await make_ua(node)
        try:
            # 1. Incoming answered call must ignore forged 200 OK with same Call-ID (direction != "out")
            incoming = []
            ua.on_incoming = incoming.append
            inv = node.request("INVITE", f"sip:line@127.0.0.1:{ua.local_port}", call_id="in-call-1",
                               from_tag="caller1", to=f"<sip:line@127.0.0.1:{ua.local_port}>", body=SDP)
            node.send(inv, ("127.0.0.1", ua.local_port))
            for _ in range(30):
                if incoming:
                    break
                await asyncio.sleep(0.02)
            assert incoming
            in_call = incoming[0]
            await in_call.accept()
            await asyncio.sleep(0.05)

            forged_200 = SipMessage(
                status=200, reason="OK",
                headers=[
                    ("Via", f"SIP/2.0/UDP 127.0.0.1:{ua.local_port};branch=z9hG4bKforged"),
                    ("From", f"<sip:line@127.0.0.1:{ua.local_port}>;tag={in_call.dialog.local_tag}"),
                    ("To", "<sip:+15550001001@127.0.0.1>;tag=eviltag"),
                    ("Call-ID", "in-call-1"),
                    ("CSeq", "1 INVITE"),
                    ("Contact", f"<sip:victim@127.0.0.1:{victim.port}>"),
                ],
                body=SDP,
            )
            node.send(forged_200, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.1)
            assert in_call.state == "answered" and not in_call.ended.done()
            assert victim.received == [] and node.count("BYE") == 0
            await in_call.hangup()
            bye_in, baddr = await node.expect("BYE")
            node.reply(bye_in, baddr, 200)

            # 2. Outgoing cancelled call ignores stray 200 OK from untrusted source (no reflection to victim)
            ua_untrusted = SipUA(SipConfig(registrar="192.0.2.99", registrar_port=5060, username="line",
                                           register=False, local_host="127.0.0.1"))
            await ua_untrusted.start()
            try:
                out_call = await ua_untrusted.dial("+15550001001", on_event=lambda *a: None)
                out_call_id = out_call.dialog.call_id
                out_local_tag = out_call.dialog.local_tag
                await out_call.cancel()
                spoof = SipMessage(
                    status=200, reason="OK",
                    headers=[
                        ("Via", f"SIP/2.0/UDP 127.0.0.1:{ua_untrusted.local_port};branch=z9hG4bKnone"),
                        ("From", f"<sip:line@192.0.2.99>;tag={out_local_tag}"),
                        ("To", "<sip:+15550001001@192.0.2.99>;tag=spoof1"),
                        ("Call-ID", out_call_id),
                        ("CSeq", "1 INVITE"),
                        ("Contact", f"<sip:victim@127.0.0.1:{victim.port}>"),
                    ],
                    body=SDP,
                )
                before_drop = ua_untrusted.stats["dropped_untrusted"]
                attacker.send(spoof, ("127.0.0.1", ua_untrusted.local_port))
                await asyncio.sleep(0.1)
                assert ua_untrusted.stats["dropped_untrusted"] == before_drop + 1
                assert victim.received == []
            finally:
                await ua_untrusted.stop()

            # 3. Late 200 OK from trusted node with malformed Contact does not leak unhandled task error
            call3 = await ua.dial("+15550001003", on_event=lambda *a: None)
            inv3, addr3 = await node.expect("INVITE")
            node.reply(inv3, addr3, 180)
            await asyncio.sleep(0.03)
            await call3.cancel()
            cancel, caddr = await node.expect("CANCEL")
            node.reply(cancel, caddr, 200)
            await asyncio.wait_for(asyncio.shield(call3.ended), 2.0)
            node.reply(inv3, addr3, 200, to_tag="latebad",
                       extra=[("Contact", "<sip:peer@127.0.0.1;port=abc>")], body=SDP)
            await asyncio.sleep(0.1)
            gc.collect()
            await asyncio.sleep(0.02)
        finally:
            await ua.stop()
            node.close()
            attacker.close()
            victim.close()

    run(scenario())
    assert errors == []


# -- A response from another address of the server, inside its transaction ---------------------------------

def test_a_response_from_another_interface_completes_its_transaction():
    # Default timers: a dropped answer would leave the BYE waiting for Timer F (32 s).
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call, invite, _ = await answered(ua, node)
            hangup = asyncio.ensure_future(call.hangup("agent_hangup"))
            bye, _ = await node.expect("BYE")
            dropped = ua.stats["dropped_untrusted"]
            ua._datagram(response_to(bye, 200).encode(), ("198.51.100.7", 5060))  # the server's other address
            await asyncio.wait_for(hangup, 1.0)
            assert call.ended.done() and call.end_reason == "agent_hangup"
            assert ua.stats["dropped_untrusted"] == dropped
            # a 2xx outside any transaction is still taken only from trusted addresses and the call's peer
            stray = response_to(invite, 200, to_tag="elsewhere", extra=[("Contact", "<sip:x@198.51.100.7>")],
                                body=SDP)
            ua._datagram(stray.encode(), ("198.51.100.7", 5060))
            await asyncio.sleep(0.05)
            assert ua.stats["dropped_untrusted"] == dropped + 1
            assert node.count("ACK", packets=True) == 1
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- Port > 65535 or 0 in Contact, Record-Route or SDP -----------------------------------------------------

def test_out_of_range_ports_never_kill_sip_or_rtp_sockets(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        rtp = await RtpEndpoint.open("127.0.0.1", lambda *a: None)
        try:
            # 1. Outgoing INVITE receives 200 OK with Contact port 99999
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, extra=[("Contact", "<sip:peer@127.0.0.1:99999>")], body=SDP)
            await asyncio.wait_for(asyncio.shield(call.ended), 2.0)
            assert not ua._udp.transport.is_closing()

            # 2. Incoming INVITE with Contact port 99999 and Record-Route port 0
            bad_inv = node.request("INVITE", f"sip:line@127.0.0.1:{ua.local_port}", call_id="bad-port-in",
                                   from_tag="f1", to=f"<sip:line@127.0.0.1:{ua.local_port}>",
                                   contact="<sip:peer@127.0.0.1:99999>",
                                   extra=[("Record-Route", "<sip:127.0.0.1:0;lr>")], body=SDP)
            node.send(bad_inv, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.05)
            assert not ua._udp.transport.is_closing()

            # SIP socket remains alive and answers OPTIONS with 200 OK
            opt = node.request("OPTIONS", f"sip:line@127.0.0.1:{ua.local_port}", call_id="opt-alive",
                               from_tag="f2", to=f"<sip:line@127.0.0.1:{ua.local_port}>")
            node.send(opt, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.05)
            assert any(not m.is_request and m.status == 200 and m.call_id == "opt-alive" for m, _ in node.received)

            # 3. SDP and RtpEndpoint reject port 0 / 99999 without closing RTP datagram transport
            for bad_port in (0, 99999):
                with pytest.raises(SdpError):
                    parse_sdp(SDP_ANSWER.format(port=bad_port))
                with pytest.raises(ValueError):
                    rtp.set_remote(("127.0.0.1", bad_port))
                rtp.remote = ("127.0.0.1", bad_port)
                rtp.send_payload(0, b"\xff" * 160)
                assert rtp.transport is not None and not rtp.transport.is_closing()
        finally:
            rtp.close()
            await ua.stop()
            node.close()

    run(scenario())


def test_learn_public_ignores_invalid_rport_and_received():
    ua = SipUA(SipConfig(registrar="127.0.0.1", username="line", register=False))
    bad = SipMessage(status=200, reason="OK", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1:5060;received=evil.example;rport=99999;branch=z9hG4bK1"),
    ])
    ua._learn_public(bad)
    assert ua.public is None
