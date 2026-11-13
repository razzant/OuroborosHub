"""Forked INVITEs: the first answer is kept, every other 2xx gets its own ACK and BYE and never disturbs
the answered dialog."""
import asyncio

from conftest import run
from phone import sip_ua
from phone.sip_ua import SipUA
from sipfake import FakeNode, SDP, contact, make_ua


def test_forked_second_2xx_does_not_clobber_active_answered_dialog(fast):
    async def scenario():
        node = await FakeNode.start()
        fork = await FakeNode.start()
        ua = await make_ua(node, trusted_sources=("127.0.0.1",))
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, to_tag="winner",
                       extra=[("Contact", f"<sip:win@127.0.0.1:{node.port}>")], body=SDP)
            await node.expect("ACK")
            await asyncio.sleep(0.03)
            assert call.state == "answered" and call.dialog.remote_tag == "winner"

            # Second forked leg answers with a different To-tag
            fork.reply(invite, ("127.0.0.1", ua.local_port), 200, to_tag="loser",
                       extra=[("Contact", f"<sip:lose@127.0.0.1:{fork.port}>")], body=SDP)
            await fork.expect("ACK")
            bye, baddr = await fork.expect("BYE")
            fork.reply(bye, baddr, 200)
            await asyncio.sleep(0.05)

            # Winning dialog stays alive and untouched
            assert call.state == "answered" and not call.ended.done()
            assert call.dialog.remote_tag == "winner"
            assert node.count("BYE") == 0
        finally:
            await ua.stop()
            node.close()
            fork.close()

    run(scenario())


def test_a_forked_2xx_with_unusable_routing_gets_ack_and_bye_and_spares_the_call(fast):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, to_tag="winner", extra=contact(node), body=SDP)
            await node.expect("ACK")
            await asyncio.sleep(0.03)
            node.reply(invite, addr, 200, to_tag="loser", extra=[("Contact", "<sip:lose@127.0.0.1")], body=SDP)
            ack, _ = await node.expect("ACK", nth=2)
            bye, baddr = await node.expect("BYE")
            assert ack.to_addr.tag == "loser" and bye.to_addr.tag == "loser"
            node.reply(bye, baddr, 200)
            await asyncio.sleep(0.05)
            assert call.state == "answered" and not call.ended.done() and call.dialog.remote_tag == "winner"
        finally:
            await ua.stop()
            node.close()

    run(scenario())


# -- A forked 2xx retransmitted while its Contact resolves -------------------------------------------------

def test_a_retransmitted_forked_2xx_never_gets_the_answering_dialogs_ack(fast, monkeypatch):
    gate = {}
    real_resolve = SipUA._resolve

    async def slow_resolve(self, host, port):
        if host == "loser-contact.test":
            gate["entered"].set()
            await gate["release"].wait()
            return "127.0.0.1", port
        return await real_resolve(self, host, port)

    monkeypatch.setattr(SipUA, "_resolve", slow_resolve)

    async def scenario():
        gate["entered"], gate["release"] = asyncio.Event(), asyncio.Event()
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, to_tag="winner", extra=contact(node), body=SDP)
            await node.expect("ACK")
            await asyncio.sleep(0.03)
            loser = node.reply(invite, addr, 200, to_tag="loser",
                               extra=[("Contact", f"<sip:lose@loser-contact.test:{node.port}>")], body=SDP)
            await asyncio.wait_for(gate["entered"].wait(), 2)
            node.send(loser, addr)  # retransmitted while the loser's Contact is still being resolved
            await asyncio.sleep(0.1)
            during = [m.to_addr.tag for m, _ in node.received if m.is_request and m.method == "ACK"]
            gate["release"].set()
            ack, _ = await node.expect("ACK", nth=2)
            bye, baddr = await node.expect("BYE")
            node.reply(bye, baddr, 200)
            await asyncio.sleep(0.05)
            return during, ack.to_addr.tag, bye.to_addr.tag, call.state, call.dialog.remote_tag
        finally:
            gate["release"].set()
            await ua.stop()
            node.close()

    during, ack_tag, bye_tag, state, remote_tag = run(scenario())
    assert during == ["winner"], during
    assert ack_tag == bye_tag == "loser" and state == "answered" and remote_tag == "winner"


# -- Every ACK of a forked or late 2xx carries the credentials of the INVITE it answers --------------------

def test_a_forked_or_late_2xx_gets_the_credentials_of_the_invite_it_answers(fast):
    # RFC 3261 §22.1: an ACK duplicates the Authorization and Proxy-Authorization of "the INVITE to which the ACK
    # corresponds". A second fork answering the authenticated INVITE gets its credentials; a 2xx to the first,
    # challenged INVITE (CSeq 1, sent without credentials) gets an ACK without any.
    def credentials(message):
        return [(n, v) for n, v in message.headers if n.lower() in ("authorization", "proxy-authorization")]

    async def scenario():
        node = await FakeNode.start()
        ua = await make_ua(node)
        try:
            call = await ua.dial("+15550001001", on_event=lambda *a: None)
            first, addr = await node.expect("INVITE")
            node.reply(first, addr, 407, extra=[("Proxy-Authenticate", 'Digest realm="p", nonce="n7", qop="auth"')])
            second, addr = await node.expect("INVITE", nth=2)
            node.reply(second, addr, 200, to_tag="winner", extra=contact(node), body=SDP)
            winner_ack, _ = await node.expect("ACK", nth=2)       # the first ACK is the 407's
            node.reply(second, addr, 200, to_tag="fork", extra=contact(node), body=SDP)
            fork_ack, _ = await node.expect("ACK", nth=3)
            fork_bye, baddr = await node.expect("BYE")
            node.reply(fork_bye, baddr, 200)
            await asyncio.sleep(sip_ua.TRANSACTION_TIMEOUT + 0.1)  # the 407's INVITE transaction is gone
            node.reply(first, addr, 200, to_tag="late", extra=contact(node), body=SDP)
            late_ack, _ = await node.expect("ACK", nth=4)
            late_bye, baddr = await node.expect("BYE", nth=2)
            node.reply(late_bye, baddr, 200)
            await asyncio.sleep(0.05)
            return first, second, winner_ack, fork_ack, late_ack, call.state, call.dialog.remote_tag
        finally:
            await ua.stop()
            node.close()

    first, second, winner_ack, fork_ack, late_ack, state, remote_tag = run(scenario())
    assert credentials(second) and credentials(first) == []
    assert credentials(winner_ack) == credentials(fork_ack) == credentials(second)
    assert fork_ack.to_addr.tag == "fork" and fork_ack.cseq == (second.cseq[0], "ACK")
    assert late_ack.to_addr.tag == "late" and late_ack.cseq == (first.cseq[0], "ACK")
    assert credentials(late_ack) == []
    assert state == "answered" and remote_tag == "winner"
