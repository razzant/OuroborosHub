"""The responses the line keeps to answer retransmitted requests (RFC 3261 §17.2): bounded in count and bytes,
each request costing a bounded amount of work whatever a burst does, a retransmission inside the bound answered
with the very same response and processed once, and one whose response was forgotten answered as a new request,
an initial INVITE by the call it already started. Requests go through the line's own datagram handler and its
check of trusted sources; the transport is a real loopback socket."""
import asyncio
import time
import types

import pytest

from phone import sip_ua
from phone.sip_message import parse as parse_sip
from phone.sip_ua import SipConfig, SipUA
from sipfake import SDP_ANSWER, FakeNode

MAX = getattr(sip_ua, "SERVER_RESPONSES_MAX", 256)
MAX_BYTES = getattr(sip_ua, "SERVER_RESPONSES_MAX_BYTES", 256 * 1024)


def run(coro):
    return asyncio.run(coro)


async def make_line(node):
    ua = SipUA(SipConfig(registrar="127.0.0.1", registrar_port=node.port, username="line", register=False,
                         local_host="127.0.0.1"), on_incoming=lambda call: node.incoming.append(call))
    await ua.start()
    node.incoming = []
    return ua


def options(node, ua, i, branch=None):
    return node.request("OPTIONS", ua.aor(), call_id=f"options-{i}", from_tag=f"f{i}", to=f"<{ua.aor()}>",
                        branch=branch or f"z9hG4bK-burst-{i}")


def held_bytes(ua):
    return sum(len(entry[0].encode()) for entry in ua._server_responses.values())


class CountingTable(dict):
    """The line's response table, counting every entry any scan of it looks at."""

    looked_at = 0

    def __iter__(self):
        for key in super().__iter__():
            CountingTable.looked_at += 1
            yield key

    def items(self):
        for item in super().items():
            CountingTable.looked_at += 1
            yield item

    def keys(self):
        return list(iter(self))

    def values(self):
        for value in super().values():
            CountingTable.looked_at += 1
            yield value


def test_a_burst_of_requests_keeps_the_table_bounded_and_each_request_cheap():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        CountingTable.looked_at = 0
        ua._server_responses = CountingTable()
        try:
            started = time.monotonic()
            for i in range(4096):
                ua._datagram(options(node, ua, i).encode(), ("127.0.0.1", node.port))
            return {"kept": len(ua._server_responses), "bytes": held_bytes(ua), "looked_at": CountingTable.looked_at,
                    "seconds": round(time.monotonic() - started, 3), "dropped": ua.stats["dropped_untrusted"]}
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    facts = run(scenario())
    assert facts["dropped"] == 0, facts                         # every request was a trusted one, handled
    assert facts["kept"] <= MAX and facts["bytes"] <= MAX_BYTES, facts
    # Each request looks at a bounded number of entries (the head of the table, a few times); the old table
    # scanned all of them on every request past 256: millions of looks for this burst.
    assert facts["looked_at"] <= 8 * 4096, facts


def test_big_responses_are_bounded_by_bytes():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        try:
            for i in range(64):  # requests near the datagram limit: their responses echo the long headers
                request = options(node, ua, i)
                request.headers.append(("Via", "SIP/2.0/UDP 127.0.0.1:9;branch=z9hG4bKpad" + "x" * 6000))
                ua._datagram(request.encode(), ("127.0.0.1", node.port))
            return len(ua._server_responses), held_bytes(ua)
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    kept, held = run(scenario())
    assert 1 <= kept < 64 and held <= MAX_BYTES, (kept, held)


def test_a_retransmission_inside_the_bound_gets_the_same_response_and_is_processed_once():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        try:
            uri = f"sip:line@127.0.0.1:{ua.local_port}"
            invite = node.request("INVITE", uri, call_id="cache-dtmf", from_tag="far", to=f"<{uri}>",
                                  body=SDP_ANSWER.format(port=40000).encode())
            node.send(invite, ("127.0.0.1", ua.local_port))
            for _ in range(200):
                if node.incoming:
                    break
                await asyncio.sleep(0.01)
            call = node.incoming[0]
            digits = []
            call.on_event = lambda c, kind, data: digits.append(data.get("digit")) if kind == "dtmf" else None
            await call.accept()
            for _ in range(200):
                ok = [m for m, _ in node.received if not m.is_request and m.status == 200 and m.cseq[1] == "INVITE"]
                if ok:
                    break
                await asyncio.sleep(0.01)
            tag = ok[0].to_addr.tag
            node.send(node.request("ACK", uri, call_id="cache-dtmf", from_tag="far", to=f"<{uri}>", to_tag=tag),
                      ("127.0.0.1", ua.local_port))
            info = node.request("INFO", uri, call_id="cache-dtmf", from_tag="far", to=f"<{uri}>", to_tag=tag,
                                cseq=2, body=b"Signal=5\r\nDuration=160\r\n",
                                extra=[("Content-Type", "application/dtmf-relay")])
            info.headers = [h for h in info.headers if h != ("Content-Type", "application/sdp")]
            for _ in range(3):  # the far end retransmits: our 200 was lost twice
                node.send(info, ("127.0.0.1", ua.local_port))
                await asyncio.sleep(0.05)
            answers = [m.encode() for m, _ in node.received if not m.is_request and m.cseq == (2, "INFO")]
            return digits, answers
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    digits, answers = run(scenario())
    assert digits == ["5"], digits
    assert len(answers) == 3 and len(set(answers)) == 1, answers


def test_a_retransmitted_invite_whose_response_was_forgotten_starts_no_second_call():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        try:
            uri = f"sip:line@127.0.0.1:{ua.local_port}"
            invite = node.request("INVITE", uri, call_id="cache-ring", from_tag="far", to=f"<{uri}>",
                                  body=SDP_ANSWER.format(port=40000).encode())
            node.send(invite, ("127.0.0.1", ua.local_port))
            for _ in range(200):
                if node.incoming:
                    break
                await asyncio.sleep(0.01)
            node.incoming[0].ringing()
            facts = {}
            for i in range(MAX + 64):  # a burst from the provider pushes the INVITE's response out
                ua._datagram(options(node, ua, i).encode(), ("127.0.0.1", node.port))
            facts["forgotten"] = (invite.top_via.branch, "INVITE") not in ua._server_responses
            await asyncio.sleep(0.1)       # the burst's answers reach the far end first
            node.received.clear()
            node.send(invite, ("127.0.0.1", ua.local_port))  # the far end retransmits its INVITE
            await asyncio.sleep(0.2)
            facts["calls"] = len(ua.calls())
            facts["offered"] = len(node.incoming)
            facts["answers"] = [m.status for m, _ in node.received if not m.is_request and m.cseq[1] == "INVITE"]
            return facts
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    facts = run(scenario())
    assert facts["forgotten"], facts
    assert facts["calls"] == 1 and facts["offered"] == 1, facts
    assert facts["answers"] == [180], facts  # the ringing it is in, as RFC 3261 §17.2.1 asks of a retransmission


def test_a_retransmitted_invite_of_a_declined_call_is_absorbed():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        try:
            uri = f"sip:line@127.0.0.1:{ua.local_port}"
            invite = node.request("INVITE", uri, call_id="cache-busy", from_tag="far", to=f"<{uri}>",
                                  body=SDP_ANSWER.format(port=40000).encode())
            node.send(invite, ("127.0.0.1", ua.local_port))
            for _ in range(200):
                if node.incoming:
                    break
                await asyncio.sleep(0.01)
            await node.incoming[0].reject(486)        # its 486 is retransmitted on Timer G until an ACK
            for i in range(MAX + 64):
                ua._datagram(options(node, ua, i).encode(), ("127.0.0.1", node.port))
            assert (invite.top_via.branch, "INVITE") not in ua._server_responses
            node.send(invite, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.2)
            return len(node.incoming), [c for c in ua.calls()]
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    offered, live = run(scenario())
    assert offered == 1 and live == [], (offered, live)


def test_a_retransmitted_options_after_its_response_was_forgotten_is_answered_again():
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        try:
            first = options(node, ua, "first", branch="z9hG4bK-first")
            ua._datagram(first.encode(), ("127.0.0.1", node.port))
            for i in range(MAX + 64):
                ua._datagram(options(node, ua, i).encode(), ("127.0.0.1", node.port))
            await asyncio.sleep(0.1)       # the burst's answers reach the far end first
            node.received.clear()
            ua._datagram(first.encode(), ("127.0.0.1", node.port))
            await asyncio.sleep(0.1)
            return [m.status for m, _ in node.received if not m.is_request and m.call_id == "options-first"]
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    assert run(scenario()) == [200]


@pytest.mark.parametrize("acknowledged", [False, True])
def test_an_answered_invite_after_eviction_never_starts_a_second_call(acknowledged):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        try:
            uri = f"sip:line@127.0.0.1:{ua.local_port}"
            invite = node.request("INVITE", uri, call_id="cache-answered", from_tag="far", to=f"<{uri}>",
                                  body=SDP_ANSWER.format(port=40000).encode())
            node.send(invite, ("127.0.0.1", ua.local_port))
            for _ in range(200):
                if node.incoming:
                    break
                await asyncio.sleep(0.01)
            call = node.incoming[0]
            await call.accept()
            first_answer = ua._server_responses[(invite.top_via.branch, "INVITE")][0].encode()
            if acknowledged:
                ack = node.request("ACK", uri, call_id=invite.call_id, from_tag="far", to=f"<{uri}>",
                                   to_tag=call.dialog.local_tag)
                ua._datagram(ack.encode(), ("127.0.0.1", node.port))
                assert call._pending_ok == {}
            for i in range(MAX + 64):
                ua._datagram(options(node, ua, i).encode(), ("127.0.0.1", node.port))
            assert (invite.top_via.branch, "INVITE") not in ua._server_responses
            await asyncio.sleep(0.1)
            node.received.clear()
            node.send(invite, ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.2)
            answers = [m.encode() for m, _ in node.received if not m.is_request and m.cseq[1] == "INVITE"]
            assert len(ua.calls()) == 1 and node.incoming == [call]
            if acknowledged:
                assert answers == []  # the acknowledged dialog absorbs a late initial INVITE
            else:
                assert answers and all(answer == first_answer for answer in answers)
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    run(scenario())


def test_expiration_and_response_replacement_keep_byte_accounting_exact(monkeypatch):
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        try:
            clock = [time.monotonic()]
            with monkeypatch.context() as mp:
                mp.setattr(sip_ua, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
                first = options(node, ua, 0)
                for i in range(8):
                    ua._datagram(options(node, ua, i).encode(), ("127.0.0.1", node.port))
                # A newer response on the same transaction replaces the old byte count, including its body.
                ua._respond(first, 200, ("127.0.0.1", node.port), body=b"new body" * 100)
                assert ua._server_response_bytes == held_bytes(ua)
                clock[0] += sip_ua.TRANSACTION_TIMEOUT + 0.01
                ua._datagram(first.encode(), ("127.0.0.1", node.port))
                assert len(ua._server_responses) == 1
                assert ua._server_response_bytes == held_bytes(ua)
                assert ua._server_responses[(first.top_via.branch, "OPTIONS")][0].body == b""
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    run(scenario())


# -- After the call: its retransmitted requests, answered only where their response went -------------------

async def incoming(node, ua, call_id):
    uri = f"sip:line@127.0.0.1:{ua.local_port}"
    invite = node.request("INVITE", uri, call_id=call_id, from_tag="far", to=f"<{uri}>",
                          body=SDP_ANSWER.format(port=40000).encode())
    before = len(node.incoming)
    node.send(invite, ("127.0.0.1", ua.local_port))
    for _ in range(200):
        if len(node.incoming) > before:
            break
        await asyncio.sleep(0.01)
    return uri, invite, node.incoming[-1]


async def node_response(node, cseq, status):
    for _ in range(200):
        found = [m for m, _ in node.received if not m.is_request and m.cseq == cseq and m.status == status]
        if found:
            return found[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {status} for {cseq}")


def test_a_bye_repeated_after_its_call_ended_gets_its_200_again_only_at_the_address_it_came_from():
    # The far end sends from an address only its call trusted (a Contact host outside the provider's addresses).
    # Its BYE ends the call; that 200 is lost, and the BYE comes again after the call has left the line.
    async def scenario():
        node = await FakeNode.start()
        ua = await make_line(node)
        sent = []
        real_send = ua._send
        ua._send = lambda message, addr: (sent.append((message, addr)), real_send(message, addr))[-1]
        try:
            uri, invite, call = await incoming(node, ua, "bye-again")
            await call.accept()
            ok = await node_response(node, (1, "INVITE"), 200)
            node.send(node.request("ACK", uri, call_id="bye-again", from_tag="far", to=f"<{uri}>",
                                   to_tag=ok.to_addr.tag), ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.05)
            far, elsewhere = ("192.0.2.7", 5070), ("192.0.2.8", 5070)   # TEST-NET-1: never a trusted source
            call.peer_ips.add(far[0])
            dialog = dict(call_id="bye-again", from_tag="far", to=f"<{uri}>", to_tag=ok.to_addr.tag)
            bye = node.request("BYE", uri, cseq=2, **dialog)
            ua._datagram(bye.encode(), far)
            ended = call.ended.done() and ua.calls() == []
            ua._datagram(bye.encode(), far)                               # the far end repeats it
            ua._datagram(bye.encode(), elsewhere)                         # the same bytes from another address
            ua._datagram(node.request("BYE", uri, cseq=3, **dialog).encode(), far)   # a new request after the call
            ua._datagram(bye.encode().replace(b"Via: SIP/2.0/UDP", b"Via: junk"), far)  # unreadable: silence
            byes = [(m.status, a) for m, a in sent if not m.is_request and m.cseq[1] == "BYE"]
            return ended, byes, ua.stats["dropped_untrusted"]
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()

    ended, byes, dropped = run(scenario())
    far = ("192.0.2.7", 5070)
    assert ended and byes == [(200, far), (200, far)], byes   # the same 200 again, to the address it went to
    assert dropped == 3                                       # trust is not widened: the rest is dropped as before


def test_a_cancel_that_crosses_the_decline_of_its_invite_gets_200_and_changes_nothing():
    # RFC 3261 §9.2: the declined INVITE's server transaction stands while its final response does (retransmitted
    # until the ACK); a CANCEL that matches it is answered 200 and has no effect. A CANCEL of a declined INVITE that
    # comes from another address than that INVITE, or names no INVITE, still gets 481.
    async def scenario():
        node = await FakeNode.start()
        other = await FakeNode.start()
        ua = await make_line(node)
        try:
            uri, invite, call = await incoming(node, ua, "decline-cancel")
            await call.reject(486)
            node.send(node.request("CANCEL", uri, call_id="decline-cancel", from_tag="far", to=f"<{uri}>",
                                   branch=invite.top_via.branch), ("127.0.0.1", ua.local_port))
            _, invite_b, call_b = await incoming(node, ua, "decline-cancel-elsewhere")
            await call_b.reject(486)
            other.send(node.request("CANCEL", uri, call_id="decline-cancel-elsewhere", from_tag="far", to=f"<{uri}>",
                                    branch=invite_b.top_via.branch), ("127.0.0.1", ua.local_port))
            node.send(node.request("CANCEL", uri, call_id="decline-cancel", from_tag="far", to=f"<{uri}>",
                                   branch="z9hG4bK-no-such-invite"), ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.8)                                       # Timer G resends the 486 meanwhile
            node.send(node.request("ACK", uri, call_id="decline-cancel", from_tag="far", to=f"<{uri}>",
                                   to_tag=call.dialog.local_tag, branch=invite.top_via.branch),
                      ("127.0.0.1", ua.local_port))
            await asyncio.sleep(0.05)

            def statuses(peer, branch, method):
                return [m.status for m, _ in peer.received
                        if not m.is_request and m.top_via.branch == branch and m.cseq[1] == method]
            return (statuses(node, invite.top_via.branch, "CANCEL"), statuses(other, invite_b.top_via.branch, "CANCEL"),
                    statuses(node, "z9hG4bK-no-such-invite", "CANCEL"),
                    sorted(set(statuses(node, invite.top_via.branch, "INVITE"))),
                    invite.top_via.branch in ua._final_retransmits, invite_b.top_via.branch in ua._final_retransmits)
        finally:
            await ua.stop(time.monotonic() + 0.2)
            node.close()
            other.close()

    matched, elsewhere, unknown, invite_finals, timer_g, timer_g_b = run(scenario())
    assert matched == [200] and elsewhere == [481] and unknown == [481], (matched, elsewhere, unknown)
    assert invite_finals == [100, 486], invite_finals    # no 487: the decline stands
    assert not timer_g and timer_g_b                     # its ACK ends Timer G; the other one still waits for its own
