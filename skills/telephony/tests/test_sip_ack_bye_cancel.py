"""Ending SIP calls and answering a 2xx: the ACK (also when its address has to be resolved first), BYE and
CANCEL under retransmission and loss, stopping the line in the middle of a dial, and a 2xx the line
cannot use."""
import asyncio
import gc

import pytest

from conftest import run
from phone.sip_message import response_to
from phone.sip_ua import SipUA
from sipfake import FakeNode, SDP, answered, contact, make_ua


UNUSABLE_SDP = b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 99\r\n"


# -- General call lifecycle across every await boundary ----------------------------------------------------

def test_sip_hangup_during_dns_before_ack_leaves_nothing_open(fast, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    real_resolve = SipUA._resolve

    async def gated_resolve(self, host: str, port: int):
        if host == "ack-race.test":
            entered.set()
            await release.wait()
            return "127.0.0.1", port
        return await real_resolve(self, host, port)

    monkeypatch.setattr(SipUA, "_resolve", gated_resolve)

    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 180)
            await asyncio.sleep(0.03)
            node.reply(invite, addr, 200, extra=[("Contact", f"<sip:peer@ack-race.test:{node.port}>")], body=SDP)
            await asyncio.wait_for(entered.wait(), 2.0)
            await call.hangup("agent_hangup")
            await asyncio.sleep(0.03)
            assert node.count("CANCEL") == 0  # final 200 already arrived: must end by ACK + BYE, not CANCEL
            release.set()
            await node.expect("ACK")
            bye, baddr = await node.expect("BYE")
            node.reply(bye, baddr, 200)
            await asyncio.wait_for(asyncio.shield(call.ended), 2.0)
            for _ in range(30):
                bg = getattr(ua, "_bg_tasks", ())
                if not [t for t in bg if t not in ua._tasks and not t.done()]:
                    break
                await asyncio.sleep(0.02)
            assert call.rtp.transport is None
            assert ua._calls == {}
            assert [t for t in getattr(ua, "_bg_tasks", ()) if t not in ua._tasks and not t.done()] == []
        finally:
            release.set()
            await ua.stop()
            node.close()

    run(scenario())


# -- Retransmitted 2xx during DNS resolution for ACK -------------------------------------------------------

def test_retransmitted_2xx_during_ack_dns_keeps_call_alive(fast, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    real_resolve = SipUA._resolve

    async def slow_resolve(self, host: str, port: int):
        if host == "slow-contact.test":
            entered.set()
            await release.wait()
            return "127.0.0.1", port
        return await real_resolve(self, host, port)

    monkeypatch.setattr(SipUA, "_resolve", slow_resolve)

    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            events = []
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append(k))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 180)
            await asyncio.sleep(0.03)
            ok = node.reply(invite, addr, 200, extra=[("Contact", f"<sip:peer@slow-contact.test:{node.port}>")],
                            body=SDP)
            await asyncio.wait_for(entered.wait(), 2.0)
            # Retransmitted 200 OK arrives while _invite_answered awaits DNS for ACK
            node.send(ok, addr)
            await asyncio.sleep(0.03)
            release.set()
            await node.expect("ACK")
            await asyncio.sleep(0.1)
            assert call.state == "answered" and "answered" in events and not call.ended.done()
            assert node.count("BYE") == 0
        finally:
            release.set()
            await ua.stop()
            node.close()

    run(scenario())


def test_a_2xx_retransmitted_after_the_ack_dns_gets_the_same_ack_again(fast, monkeypatch):
    entered, release = {}, {}
    real_resolve = SipUA._resolve

    async def slow_resolve(self, host, port):
        if host == "slow-contact.test":
            entered["event"].set()
            await release["event"].wait()
            return "127.0.0.1", port
        return await real_resolve(self, host, port)

    monkeypatch.setattr(SipUA, "_resolve", slow_resolve)

    async def scenario():
        entered["event"], release["event"] = asyncio.Event(), asyncio.Event()
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            ok = node.reply(invite, addr, 200, extra=[("Contact", f"<sip:peer@slow-contact.test:{node.port}>")],
                            body=SDP)
            await asyncio.wait_for(entered["event"].wait(), 2)
            node.send(ok, addr)  # retransmitted while the ACK's address is being resolved
            await asyncio.sleep(0.05)
            assert node.count("ACK", packets=True) == 0 and not call.ended.done()
            release["event"].set()
            first, _ = await node.expect("ACK")
            node.send(ok, addr)  # retransmitted again: the same ACK goes out again
            for _ in range(50):
                if node.count("ACK", packets=True) == 2:
                    break
                await asyncio.sleep(0.01)
            acks = [m for m, _ in node.received if m.is_request and m.method == "ACK"]
            assert len(acks) == 2 and acks[0].encode() == acks[1].encode() == first.encode()
            assert call.state == "answered" and node.count("BYE") == 0
        finally:
            release["event"].set()
            await ua.stop()
            node.close()

    run(scenario())


# -- 2xx ACK carries Authorization / Proxy-Authorization from INVITE ---------------------------------------

PROXY_CHALLENGE = [("Proxy-Authenticate", 'Digest realm="proxy.test", nonce="n1", qop="auth"')]
WWW_CHALLENGE = [("WWW-Authenticate", 'Digest realm="reg.test", nonce="n2", qop="auth"')]


def credentials(message):
    """Authorization and Proxy-Authorization headers exactly as they went over the wire, in order."""
    return [(name, value) for name, value in message.headers
            if name.lower() in ("authorization", "proxy-authorization")]


def test_2xx_ack_carries_invite_authorization_headers(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            inv1, addr = await node.expect("INVITE", nth=1)
            node.reply(inv1, addr, 407, extra=PROXY_CHALLENGE)
            ack1, _ = await node.expect("ACK", nth=1)
            inv2, addr = await node.expect("INVITE", nth=2)
            node.reply(inv2, addr, 401, extra=WWW_CHALLENGE)
            ack2, _ = await node.expect("ACK", nth=2)
            inv3, addr = await node.expect("INVITE", nth=3)
            assert inv3.get("Proxy-Authorization") and inv3.get("Authorization")
            node.reply(inv3, addr, 200, extra=[("Contact", f"<sip:peer@127.0.0.1:{node.port}>")], body=SDP)
            ack, _ = await node.expect("ACK", nth=3)
            assert "Proxy-Authorization" in dict(ack.headers)
            assert "Authorization" in dict(ack.headers)
            # RFC 3261 §22.1, §13.2.2.4: an ACK duplicates the credentials of the INVITE it acknowledges, never a
            # digest of its own; the ACKs of the 407 and the 401 likewise carry those of their own INVITEs.
            assert credentials(ack) == credentials(inv3)
            assert credentials(ack2) == credentials(inv2) and [n for n, _ in credentials(ack2)] == ["Proxy-Authorization"]
            assert credentials(ack1) == credentials(inv1) == []
            assert call.state == "answered"
        finally:
            await ua.stop()
            node.close()

    run(scenario())


def test_a_failure_ack_carries_both_credentials_of_its_invite_and_nothing_else_goes_out(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            inv1, addr = await node.expect("INVITE", nth=1)
            node.reply(inv1, addr, 407, extra=PROXY_CHALLENGE)
            inv2, addr = await node.expect("INVITE", nth=2)
            node.reply(inv2, addr, 401, extra=WWW_CHALLENGE)
            inv3, addr = await node.expect("INVITE", nth=3)
            node.reply(inv3, addr, 486)
            ack, _ = await node.expect("ACK", nth=3)
            await asyncio.wait_for(asyncio.shield(call.ended), 2)
            await asyncio.sleep(0.1)
            requests = [m for m, _ in node.received if m.is_request]
            return inv3, ack, requests, call.end_reason, node.count("INVITE"), node.count("ACK")
        finally:
            await ua.stop()
            node.close()

    inv3, ack, requests, reason, invites, acks = run(scenario())
    assert [n for n, _ in credentials(inv3)] == ["Proxy-Authorization", "Authorization"]
    assert credentials(ack) == credentials(inv3)            # the very values sent, not recomputed
    assert ack.top_via.branch == inv3.top_via.branch and ack.cseq == (inv3.cseq[0], "ACK")
    assert reason == "busy" and invites == 3 and acks == 3
    assert {m.method for m in requests} == {"INVITE", "ACK"}  # no request of its own is added for the ACKs


# -- A 2xx we cannot use still gets its ACK and a BYE ------------------------------------------------------

@pytest.mark.parametrize("flaw", ["contact", "record_route", "no_to_tag"])
def test_an_unusable_2xx_gets_its_ack_and_a_bye(fast, flaw):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            extra, to_tag = contact(node), "r1"
            if flaw == "contact":
                extra = [("Contact", "<sip:broken@127.0.0.1")]
            elif flaw == "record_route":
                extra = extra + [("Record-Route", "<sip:proxy.example;lr")]
            else:
                to_tag = ""
            ok = response_to(invite, 200, to_tag=to_tag, extra=extra, body=SDP)
            node.send(ok, addr)
            ack, _ = await node.expect("ACK")
            bye, baddr = await node.expect("BYE")
            assert ack.call_id == bye.call_id == invite.call_id and ack.cseq == (invite.cseq[0], "ACK")
            assert ack.uri == invite.uri == bye.uri  # routed the way the INVITE went
            node.reply(bye, baddr, 200 if to_tag else 481)
            await asyncio.wait_for(asyncio.shield(call.ended), 2)
            assert call.end_reason == "bad_response" and call.rtp.transport is None and ua._calls == {}
            node.send(ok, addr)  # the callee retransmits its 2xx
            await asyncio.sleep(0.1)
            assert node.count("BYE") == 1
            assert node.count("ACK", packets=True) == (2 if to_tag else 1)
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- SipParseError in _run_invite/_invite_answered and DigestError in hangup end the call cleanly ----------

def test_malformed_2xx_contact_and_bye_digest_error_finish_cleanly(fast):
    errors = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            # 1. Malformed Contact (< without >) in 200 OK: ACK and BYE, then a clean end
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, extra=[("Contact", "<sip:broken@127.0.0.1")], body=SDP)
            await node.expect("ACK")
            bye1, baddr1 = await node.expect("BYE")
            node.reply(bye1, baddr1, 200)
            await asyncio.wait_for(asyncio.shield(call.ended), 2.0)
            assert call.end_reason == "bad_response"
            assert call.rtp.transport is None
            assert ua._calls == {}

            # 2. Answered call whose BYE receives 401 with MD5-sess without qop (DigestError)
            call2 = await ua.dial("+15550001002", on_event=lambda *a: None)
            inv2, addr2 = await node.expect("INVITE", nth=2)
            node.reply(inv2, addr2, 200, extra=[("Contact", f"<sip:peer@127.0.0.1:{node.port}>")], body=SDP)
            await node.expect("ACK", nth=2)
            await asyncio.sleep(0.03)

            async def answer_bye_with_bad_digest():
                bye, baddr = await node.expect("BYE", nth=2)
                node.reply(bye, baddr, 401, extra=[
                    ("WWW-Authenticate", 'Digest realm="pbx", nonce="abc", algorithm=MD5-sess')
                ])

            responder = asyncio.ensure_future(answer_bye_with_bad_digest())
            await call2.hangup("agent_hangup")
            await responder
            assert call2.ended.done() and ua._calls == {}
            await asyncio.sleep(0.05)
            gc.collect()
            await asyncio.sleep(0.02)
        finally:
            await ua.stop()
            node.close()

    run(scenario())
    assert errors == []


# -- One BYE however many hang-ups come while it is in flight ----------------------------------------------

def test_repeated_hangups_while_the_bye_is_in_flight_send_one_bye(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call, _, _ = await answered(ua, node)
            hangups = [asyncio.ensure_future(call.hangup(reason))
                       for reason in ("agent_hangup", "time_limit", "call_closed")]
            bye, baddr = await node.expect("BYE")
            await asyncio.sleep(0.25)  # several retransmission intervals of the BYE
            assert node.count("BYE") == 1  # one transaction: retransmissions share its branch
            assert not any(h.done() for h in hangups)  # every caller waits for that one BYE
            node.reply(bye, baddr, 200)
            await asyncio.wait_for(asyncio.gather(*hangups), 1.0)
            assert call.end_reason == "agent_hangup" and node.count("BYE") == 1
            await call.hangup("again")
            assert node.count("BYE") == 1
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- A BYE started before the answer is waited for ---------------------------------------------------------

def test_hangup_waits_for_a_bye_that_started_before_the_answer():
    # Default timers: the BYE would retransmit for 32 s; the other side answers it after 0.3 s.
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 180, to_tag="r1")
            await asyncio.sleep(0.05)
            node.reply(invite, addr, 200, to_tag="r1", extra=contact(node), body=UNUSABLE_SDP)
            await node.expect("ACK")
            bye, baddr = await node.expect("BYE")  # the answer is unusable: ACK and BYE, the call still rings
            state = call.state
            hangup = asyncio.ensure_future(call.hangup("shutdown"))
            await asyncio.sleep(0.3)
            waited = not hangup.done()
            node.reply(bye, baddr, 200)
            await asyncio.wait_for(hangup, 1)
            return state, waited, call.ended.done(), call.end_reason, node.count("BYE")
        finally:
            await ua.stop()
            node.close()

    state, waited, ended, reason, byes = run(scenario())
    assert state == "ringing"
    assert waited, "hangup returned while the BYE was still unanswered"
    assert ended and reason == "bad_sdp" and byes == 1


# -- No CANCEL before a provisional, and no wait for Timer B -----------------------------------------------

def test_a_cancel_before_any_response_ends_the_call_now_and_a_late_answer_is_still_closed():
    # Default timers: Timer B would hold the call for 32 s.
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        events = []
        try:
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append(k))
            invite, addr = await node.expect("INVITE")
            await call.cancel("no_answer_timeout")
            facts = {"ended_at_once": call.ended.done(), "reason": call.end_reason,
                     "media_closed": call.rtp.transport is None, "cancels_before_1xx": node.count("CANCEL")}
            node.reply(invite, addr, 180, to_tag="late")  # a provisional after all: now the CANCEL may go
            cancel, caddr = await node.expect("CANCEL")
            node.reply(cancel, caddr, 200)
            node.reply(invite, addr, 200, to_tag="late", extra=contact(node), body=SDP)  # picked up anyway
            ack, _ = await node.expect("ACK")
            bye, baddr = await node.expect("BYE")
            node.reply(bye, baddr, 200)
            await asyncio.sleep(0.1)
            facts.update(ack_tag=ack.to_addr.tag, bye_tag=bye.to_addr.tag, events=list(events))
            return facts
        finally:
            await ua.stop()
            node.close()

    facts = run(scenario())
    assert facts["ended_at_once"] and facts["reason"] == "no_answer_timeout" and facts["media_closed"], facts
    assert facts["cancels_before_1xx"] == 0
    assert facts["ack_tag"] == facts["bye_tag"] == "late"
    assert facts["events"].count("ended") == 1 and "answered" not in facts["events"]


# -- SipUA.stop() in the middle of an outgoing dial --------------------------------------------------------

@pytest.mark.parametrize("provisional", [180, None])
def test_stopping_the_line_mid_dial_closes_the_media_port_and_ends_the_call(fast, provisional):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        stopped = False
        try:
            events = []
            call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append(k))
            invite, addr = await node.expect("INVITE")
            if provisional:
                node.reply(invite, addr, provisional)
            await asyncio.sleep(0.05)
            await ua.stop()
            stopped = True
            assert call.ended.done() and call.end_reason == "shutdown"
            assert call.rtp.transport is None and ua._calls == {}
            assert events.count("ended") == 1
        finally:
            if not stopped:
                await ua.stop()
            node.close()

    run(scenario())
