"""A phone call placed from the owner's own live call (mode S) while the phone rings: the line joins the owner's
call before it dials, so the owner hanging up reaches it at once and the dial is cancelled the way SIP allows at
that moment (nothing dialled yet, CANCEL after a provisional, ACK and BYE for a 2xx); nothing passes either way
before the answer, and there is only ever one peer. The owner's call is the host's real LiveCallHub behind its
real socket pump (serve_peer) under uvicorn; the line is the real call manager, SIP user agent and RTP against a
scripted SIP peer on loopback. Only the HTTP admission in front of the hub is a stand-in, refusing a closed call
as the host's door does."""
import asyncio
import contextlib
import copy
import math
import socket
import struct
import time

import pytest

from phone import config as cfg
from phone import g711, sip_ua
from phone.manager import CallManager
from phone.peer import HostClient
from phone.rtp import RtpPacket, build_packet, parse_packet
from phone.sdp import parse as parse_sdp
from sipfake import SDP_ANSWER, FakeNode

TOKEN = "owner-ring-token"
OWNER_CALL = "owner-call-ring"
NUMBER = "+15550001001"


def run(coro):
    return asyncio.run(coro)


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


@contextlib.asynccontextmanager
async def owner_call():
    """The owner's live call: (HostClient for the line, the hub, what the doors saw)."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute
    from ouroboros import call_protocol as cp
    from ouroboros.call_peers import CallPeerError, LiveCallHub
    from ouroboros.gateway.call_ws import serve_peer

    hub = LiveCallHub(cp.CallSpec(OWNER_CALL, 1, "test", "m", opened_by=cp.OPENED_BY_OWNER))
    seen = {"opens": 0, "sockets": 0}

    async def call_open(request):
        body = await request.json()
        if request.headers.get("x-skill-token") != TOKEN:
            return JSONResponse({"ok": False, "code": "call_peer_forbidden", "error": "x"}, status_code=403)
        if body.get("call_id") != OWNER_CALL or hub.closed:
            return JSONResponse({"ok": False, "code": "call_not_found", "error": "call_not_found"}, status_code=404)
        hub.grant_peer("telephony", "digest", mic=body.get("mic", True), speaker=body.get("speaker", False))
        seen["opens"] += 1
        return JSONResponse({"call_id": OWNER_CALL, "peer_path": f"/call/{OWNER_CALL}/peer"})

    async def peer(websocket):
        try:  # as the host's door: a closed call or a missing grant refuses the socket
            if websocket.headers.get("x-skill-token") != TOKEN:
                raise CallPeerError("call_peer_forbidden")
            grant = hub.peer_grant("telephony", "digest")
        except CallPeerError:
            await websocket.close(code=1008, reason="call_peer_forbidden")
            return
        seen["sockets"] += 1
        await serve_peer(websocket, hub, role=cp.PEER_MEETING, skill="telephony", mic=grant.mic,
                         speaker=grant.speaker)

    app = Starlette(routes=[Route("/call/open", call_open, methods=["POST"]),
                            WebSocketRoute("/call/{call_id}/peer", peer)])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", access_log=False))
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    try:
        assert await until(lambda: server.started, 5)
        yield HostClient(f"http://127.0.0.1:{sock.getsockname()[1]}", TOKEN), hub, seen
    finally:
        hub.close("test_cleanup")
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


class Phone:
    """The far end's media socket: what the line sends it, decoded."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.packets = []

    def sdp(self):
        return SDP_ANSWER.format(port=self.port).encode()

    def drain(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except BlockingIOError:
                return self.packets
            packet = parse_packet(data)
            if packet is not None and packet.payload_type == 0:
                self.packets.append(struct.unpack(f"<{len(packet.payload)}h", g711.decode(packet.payload, "PCMU")))

    def loud(self):
        return sum(1 for samples in self.drain() if max(abs(x) for x in samples) > 1000)

    def close(self):
        self.sock.close()


def line_config(node):
    config = copy.deepcopy(cfg.DEFAULTS)
    config["sip"].update(registrar="127.0.0.1", port=node.port, username="line", register=False,
                         local_host="127.0.0.1", sdp_address="local")
    config["outbound"]["allowed_prefixes"] = ["+1555"]
    return config


def tone(freq, seconds=0.4, rate=24000):
    return struct.pack(f"<{int(rate * seconds)}h",
                       *[int(9000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(rate * seconds))])


def speak(hub, item, text, freq):
    """The voice engine says something in the owner's call (to everyone in it)."""
    hub.send_caption("ouroboros", "", False, item)
    hub.play(tone(freq), item)
    hub.send_caption("ouroboros", text, True, item)


def events(hub):
    out = []
    while True:
        event = hub.next_event(0)
        if event is None:
            return out
        out.append(event)


async def answer_endings(node):
    """The far end answers each BYE and CANCEL it gets."""
    answered = set()
    while True:
        for message, addr in list(node.received):
            if (message.is_request and message.method in ("BYE", "CANCEL")
                    and message.top_via.branch not in answered):
                answered.add(message.top_via.branch)
                node.reply(message, addr, 200)
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def scene(tmp_path):
    node = await FakeNode.start()
    phone = Phone()
    responder = asyncio.ensure_future(answer_endings(node))
    async with owner_call() as (host, hub, seen):
        manager = CallManager(tmp_path, line_config(node), "", host)
        await manager.start()
        try:
            yield manager, node, hub, seen, phone
        finally:
            await manager.stop()
    responder.cancel()
    await asyncio.gather(responder, return_exceptions=True)
    node.close()
    phone.close()


async def dial(manager):
    return await manager.dial(number=NUMBER, purpose="book a table", owner_approved=True, live_call_id=OWNER_CALL)


def test_the_line_joins_before_the_dial_and_nothing_crosses_before_the_answer(tmp_path, host_server):
    async def scenario():
        facts = {}
        async with scene(tmp_path) as (manager, node, hub, seen, phone):
            result = await dial(manager)
            facts["dialled"] = result["ok"]
            facts["joined_before_invite"] = (seen["sockets"], len(hub.peers()))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 183, body=phone.sdp())
            await asyncio.sleep(0.1)
            events(hub)                                   # the join itself
            speak(hub, "to-owner", "Calling them now.", 440)   # said to the owner while it rings
            media = parse_sdp(invite.body.decode())
            for seq in range(10):  # early line audio must neither reach the hub nor play after the answer
                packet = RtpPacket(0, seq, seq * 160, 0x3333, g711.encode(tone(700, 0.02, 8000), "PCMU"))
                phone.sock.sendto(build_packet(packet), (media.address, media.port))
            await asyncio.sleep(0.5)
            ringing = events(hub)
            facts["line_audio_while_ringing"] = sum(type(e).__name__ == "PeerAudio" for e in ringing)
            facts["rtp_while_ringing"] = len(phone.drain())
            node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")], body=phone.sdp())
            await node.expect("ACK")
            await asyncio.sleep(0.4)
            facts["loud_after_answer"] = phone.loud()     # the words said to the owner are not played to them
            answered_events = events(hub)
            facts["early_line_audio_after_answer"] = any(any(e.pcm) for e in answered_events
                                                          if type(e).__name__ == "PeerAudio")
            speak(hub, "to-them", "Hello, I am an AI agent.", 1000)
            await asyncio.sleep(0.8)
            facts["loud_when_spoken"] = phone.loud()
            status = manager.status()["active_call"]
            facts["transcript"] = [row["text"] for row in status["transcript"]]
            facts["notices"] = [e.message.get("text", "")[:120] for e in answered_events + events(hub)
                                if type(e).__name__ == "PeerControl" and e.message.get("type") == "context"]
            facts["sockets"], facts["opens"] = seen["sockets"], seen["opens"]
            await manager.hangup("agent_hangup")
        return facts

    facts = run(scenario())
    assert facts["dialled"] and facts["joined_before_invite"] == (1, 1), facts
    assert facts["line_audio_while_ringing"] == 0 and facts["rtp_while_ringing"] == 0, facts
    assert facts["early_line_audio_after_answer"] is False, facts
    assert facts["loud_after_answer"] == 0 and facts["loud_when_spoken"] >= 5, facts
    assert facts["transcript"] == ["Hello, I am an AI agent."], facts
    assert facts["sockets"] == 1 and facts["opens"] == 1, facts   # one peer for the whole call
    assert sum("answered the outgoing call" in text for text in facts["notices"]) == 1, facts


def test_the_owner_hanging_up_while_it_rings_cancels_the_dial(tmp_path, host_server):
    async def scenario():
        async with scene(tmp_path) as (manager, node, hub, seen, phone):
            assert (await dial(manager))["ok"]
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 180)
            await asyncio.sleep(0.1)
            closed_at = time.monotonic()
            hub.close("finished", detail="owner_hangup")
            cancel, _ = await node.expect("CANCEL", timeout=2.0)
            cancelled_in = time.monotonic() - closed_at
            node.reply(invite, addr, 487)
            await node.expect("ACK")
            freed = await until(lambda: not manager.busy, 3)
            return {"cancelled_in": cancelled_in, "freed": freed, "cancels": node.count("CANCEL"),
                    "end": manager.last.end_reason if manager.last else None, "sockets": seen["sockets"],
                    "rtp": len(phone.drain())}

    facts = run(scenario())
    assert facts["cancelled_in"] < 1.0 and facts["cancels"] == 1 and facts["freed"], facts
    assert facts["end"] == "call_closed" and facts["sockets"] == 1 and facts["rtp"] == 0, facts


@pytest.mark.parametrize("then", ["180", "200"])
def test_a_hang_up_before_any_provisional_cancels_at_the_first_one_or_byes_a_2xx(tmp_path, host_server, then):
    async def scenario():
        async with scene(tmp_path) as (manager, node, hub, seen, phone):
            assert (await dial(manager))["ok"]
            invite, addr = await node.expect("INVITE")    # no answer from the network yet
            hub.close("finished", detail="owner_hangup")
            freed = await until(lambda: not manager.busy, 2)
            await asyncio.sleep(0.3)
            facts = {"freed": freed, "cancels_before": node.count("CANCEL")}  # RFC 3261 §9.1: none may go yet
            if then == "180":
                node.reply(invite, addr, 180)
                await node.expect("CANCEL", timeout=2.0)
                node.reply(invite, addr, 487)
                await node.expect("ACK")
            else:
                node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")],
                           body=phone.sdp())
                await node.expect("ACK")
                await node.expect("BYE", timeout=2.0)
            await asyncio.sleep(0.2)
            facts.update(cancels=node.count("CANCEL"), byes=node.count("BYE"), sockets=seen["sockets"],
                         loud=phone.loud())
            return facts

    facts = run(scenario())
    assert facts["freed"] and facts["cancels_before"] == 0, facts
    expected = (1, 0) if then == "180" else (0, 1)
    assert (facts["cancels"], facts["byes"]) == expected and facts["sockets"] == 1 and facts["loud"] == 0, facts


def test_a_hang_up_as_the_2xx_arrives_gets_ack_and_one_bye(tmp_path, host_server):
    async def scenario():
        async with scene(tmp_path) as (manager, node, hub, seen, phone):
            assert (await dial(manager))["ok"]
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 180)
            await asyncio.sleep(0.1)
            speak(hub, "to-owner", "It rings.", 440)
            await asyncio.sleep(0.2)
            node.reply(invite, addr, 200, extra=[("Contact", f"<sip:far@127.0.0.1:{node.port}>")], body=phone.sdp())
            hub.close("finished", detail="owner_hangup")   # in the same moment
            await node.expect("ACK", timeout=2.0)
            await node.expect("BYE", timeout=3.0)
            freed = await until(lambda: not manager.busy, 3)
            await asyncio.sleep(0.2)
            return {"freed": freed, "byes": node.count("BYE"), "loud": phone.loud(), "sockets": seen["sockets"]}

    facts = run(scenario())
    assert facts["freed"] and facts["byes"] == 1 and facts["loud"] == 0 and facts["sockets"] <= 1, facts


def test_a_hang_up_while_the_line_joins_dials_nothing(tmp_path, host_server):
    async def scenario():
        async with scene(tmp_path) as (manager, node, hub, seen, phone):
            real_open = manager.host.open_call

            async def open_then_owner_hangs_up(**kwargs):
                call_id = await real_open(**kwargs)
                hub.close("finished", detail="owner_hangup")  # between the door's grant and the socket
                return call_id

            manager.host.open_call = open_then_owner_hangs_up
            result = await dial(manager)
            await asyncio.sleep(0.5)
            return result, node.count("INVITE"), manager.busy

    result, invites, busy = run(scenario())
    assert result["ok"] is False and invites == 0 and not busy, (result, invites)


def test_a_hang_up_while_the_line_opens_its_media_dials_nothing(tmp_path, host_server, monkeypatch):
    real_open_rtp = sip_ua.SipCall._open_rtp

    async def scenario():
        async with scene(tmp_path) as (manager, node, hub, seen, phone):
            async def open_while_the_owner_hangs_up(call):
                hub.close("finished", detail="owner_hangup")
                active = manager.active
                await until(lambda: active.peer is not None and active.peer.closed, 1.5)  # the line saw it
                await real_open_rtp(call)

            monkeypatch.setattr(sip_ua.SipCall, "_open_rtp", open_while_the_owner_hangs_up)
            result = await dial(manager)
            await asyncio.sleep(0.5)
            return result, node.count("INVITE"), manager.busy, manager.last.end_reason if manager.last else None

    result, invites, busy, end = run(scenario())
    assert result["ok"] is False and result["code"] == "PHONE_CALL_CANCELLED", result
    assert invites == 0 and not busy and end == "call_closed", (invites, busy, end)
