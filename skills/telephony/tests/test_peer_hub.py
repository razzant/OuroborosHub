"""The phone peer against the host's real LiveCallHub and socket pump (serve_peer) under uvicorn."""
import asyncio
import math
import socket
import struct
import threading
import time

import pytest

from phone import dsp
from phone.jitter import FRAME_BYTES
from phone.peer import CallPeer, HostClient, PeerRefused

TOKEN = "test-token"


def tone(freq, rate, n, amp=8000):
    return struct.pack(f"<{n}h", *[int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)])


def dominant(pcm, rate, candidates):
    samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
    def power(f):
        w = 2 * math.pi * f / rate
        c, s1, s2 = 2 * math.cos(w), 0.0, 0.0
        for x in samples:
            s1, s2 = x + c * s1 - s2, s1
        return s1 * s1 + s2 * s2 - c * s1 * s2
    return max(candidates, key=power)


@pytest.fixture()
def server(host_server):
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute
    from ouroboros import call_protocol as cp
    from ouroboros.call_peers import LiveCallHub
    from ouroboros.gateway.call_ws import serve_peer

    hubs = {}
    opened = []

    async def call_open(request):
        if request.headers.get("x-skill-token") != TOKEN:
            return JSONResponse({"ok": False, "code": "call_peer_forbidden", "error": "x"}, status_code=403)
        body = await request.json()
        opened.append(body)
        if body.get("event") and body["event"]["conversation_id"] == "+19999999999":
            return JSONResponse({"ok": False, "code": "place_silent", "error": "silent"}, status_code=403)
        call_id = body.get("call_id") or "presence-1"
        hub = hubs.setdefault(call_id, LiveCallHub(cp.CallSpec(call_id, 1, "test", "m", opened_by=cp.OPENED_BY_SKILL)))
        hub.grant_peer("telephony", "d", mic=body.get("mic", True), speaker=body.get("speaker", False))
        return JSONResponse({"call_id": call_id, "peer_path": f"/call/{call_id}/peer"})

    async def peer(websocket):
        if websocket.headers.get("x-skill-token") != TOKEN:
            await websocket.close(code=1008)
            return
        hub = hubs[websocket.path_params["call_id"]]
        grant = hub.peer_grant("telephony", "d")
        await serve_peer(websocket, hub, role=cp.PEER_MEETING, skill="telephony", mic=grant.mic, speaker=grant.speaker)

    app = Starlette(routes=[Route("/call/open", call_open, methods=["POST"]),
                            WebSocketRoute("/call/{call_id}/peer", peer)])
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "hubs": hubs, "opened": opened}
    srv.should_exit = True
    thread.join(5)


async def drain(hub, kinds, timeout=2.0):
    """Hub events of the given class names, collected until quiet."""
    out = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = await asyncio.to_thread(hub.next_event, 0.1)
        if event is None:
            if out:
                break
            continue
        if type(event).__name__ in kinds:
            out.append(event)
    return out


def test_open_call_refusals_and_grant(server):
    async def scenario():
        host = HostClient(server["url"], TOKEN)
        assert await host.open_call(label="Phone", call_id="owner-1") == "owner-1"
        assert server["opened"][-1] == {"role": "meeting", "mic": True, "speaker": True, "label": "Phone",
                                        "call_id": "owner-1"}
        with pytest.raises(PeerRefused) as err:
            await host.open_call(label="Phone", event={"conversation_id": "+19999999999"})
        assert err.value.code == "place_silent" and err.value.status == 403
        with pytest.raises(PeerRefused) as err:
            await HostClient(server["url"], "wrong").open_call(label="x", call_id="owner-1")
        assert err.value.code == "call_peer_forbidden"
    asyncio.run(scenario())


def test_bridge_audio_both_ways_played_flush_and_close(server):
    from ouroboros import call_protocol as cp

    async def scenario():
        host = HostClient(server["url"], TOKEN)
        call_id = await host.open_call(label="Phone +15550001001", call_id="call-A")
        hub = server["hubs"][call_id]
        closed = []
        captions = []
        peer = CallPeer(host, call_id, label="Phone +15550001001", on_closed=closed.append,
                        on_caption=captions.append)
        await peer.attach()
        assert peer.epoch == 0
        joined = await drain(hub, {"PeerJoined"})
        info = joined[0].peer
        assert (info.role, info.mic, info.speaker, info.skill) == ("meeting", True, True, "telephony")

        # engine declares Gemini-like rates: flush to epoch 1, input at 16 kHz
        hub.set_engine_rates(16000, 24000)
        await asyncio.sleep(0.2)
        assert peer.epoch == 1

        # line -> engine: 1 s of 1 kHz at 8 kHz arrives as 16 kHz engine input
        line = tone(1000, 8000, 8000)
        for offset in range(0, len(line), FRAME_BYTES):
            peer.push_line_frame(line[offset:offset + FRAME_BYTES])
            await asyncio.sleep(0.002)
        audio = b"".join(e.pcm for e in await drain(hub, {"PeerAudio"}, timeout=3))
        assert abs(len(audio) / 2 - 16000) < 1600
        assert dominant(audio[3200:], 16000, [500, 1000, 2000, 3000]) == 1000
        assert dsp.rms(audio[3200:]) > 4000

        # engine -> line: caption, then 1 s of 440 Hz at 24 kHz, played out frame by frame
        hub.send_caption("ouroboros", "", False, "item-1")
        hub.play(tone(440, 24000, 24000), "item-1")
        hub.send_caption("ouroboros", "Hello", True, "item-1")
        await asyncio.sleep(0.3)
        frames = []
        for _ in range(60):
            f = peer.pop_line_frame()
            if f is not None:
                frames.append(f)
            await asyncio.sleep(0.001)
        played_out = b"".join(frames)
        assert len(frames) == 50
        assert dominant(played_out[1600:], 8000, [220, 440, 880]) == 440
        await asyncio.sleep(0.2)
        played = [e.message for e in await drain(hub, {"PeerControl"}) if e.message["type"] == "played"]
        assert played and played[-1] == {"type": "played", "item_id": "item-1", "played_ms": 1000}
        assert any(c.get("text") == "Hello" for c in captions)

        # interruption: 2 s queued, 10 frames played, hub flushes
        hub.send_caption("ouroboros", "", False, "item-2")
        hub.play(tone(440, 24000, 48000), "item-2")
        await asyncio.sleep(0.3)
        for _ in range(10):
            assert peer.pop_line_frame() is not None
        epoch = hub.flush()
        await asyncio.sleep(0.2)
        assert peer.epoch == epoch and peer.pop_line_frame() is None and peer.stats["flushes"] >= 1
        played = [e.message for e in await drain(hub, {"PeerControl"}) if e.message["type"] == "played"]
        assert played[-1] == {"type": "played", "item_id": "item-2", "played_ms": 200}

        # old-epoch audio after the flush is dropped
        hub._epoch = (hub._epoch - 1) & 0xFFFF
        hub.play(tone(440, 24000, 4800), "item-2")
        hub._epoch = epoch
        await asyncio.sleep(0.2)
        assert peer.pop_line_frame() is None and peer.stats["out_stale"] >= 1

        peer.send_context("[Phone line notice] The other party pressed key 5.")
        await asyncio.sleep(0.2)
        context = [e.message for e in await drain(hub, {"PeerControl"}) if e.message["type"] == "context"]
        assert context and "key 5" in context[0]["text"]

        hub.close("finished", detail="end_call")
        for _ in range(50):
            if closed:
                break
            await asyncio.sleep(0.05)
        assert closed and closed[0].startswith("finished")
        await peer.close("done")

    asyncio.run(scenario())


def test_bye_leaves_the_hub(server):
    async def scenario():
        host = HostClient(server["url"], TOKEN)
        call_id = await host.open_call(label="Phone", call_id="call-B")
        hub = server["hubs"][call_id]
        peer = CallPeer(host, call_id, label="Phone")
        await peer.attach()
        await drain(hub, {"PeerJoined"})
        await peer.close("remote_hangup")
        left = await drain(hub, {"PeerLeft"})
        assert left and left[0].reason == "remote_hangup" and hub.peers() == ()
    asyncio.run(scenario())


def test_a_caption_of_any_length_never_cuts_the_call_audio(server):
    # The host's caption text has no byte cutoff; the socket also carries the audio, whose bound is one frame.
    # A caption arrives whole however long, and the audio beside it goes on.
    async def scenario():
        host = HostClient(server["url"], TOKEN)
        call_id = await host.open_call(label="Phone", call_id="call-C")
        hub = server["hubs"][call_id]
        closed, captions = [], []
        peer = CallPeer(host, call_id, label="Phone", on_closed=closed.append, on_caption=captions.append)
        await peer.attach()
        try:
            await drain(hub, {"PeerJoined"})

            async def played(item):
                hub.play(tone(440, 24000, 24000), item)
                await asyncio.sleep(0.4)
                return sum(1 for _ in range(60) if peer.pop_line_frame() is not None)

            long_text = "я" * 66000                 # 132 000 bytes of UTF-8: twice the audio frame bound
            hub.send_caption("ouroboros", long_text, True, "long-1")
            facts = {"long_frames": await played("long-1"),
                     "long_caption": [len(c["text"]) for c in captions if c.get("item_id") == "long-1"]}
            assert closed == [] and facts["long_caption"] == [66000] and facts["long_frames"] == 50, (closed, facts)
            hub.send_caption("ouroboros", "x" * (2 << 20), True, "huge-1")      # 2 MiB, still a caption
            hub.send_caption("ouroboros", "after", True, "after-1")
            facts.update(after_frames=await played("after-1"),
                         after=[c["text"] for c in captions if c.get("item_id") == "after-1"],
                         huge=[len(c["text"]) for c in captions if c.get("item_id") == "huge-1"],
                         closed=list(closed), connected=peer.connected)
            return facts
        finally:
            await peer.close("done")

    facts = asyncio.run(scenario())
    assert facts["closed"] == [] and facts["connected"], facts["closed"]
    assert facts["huge"] == [2 << 20] and facts["after"] == ["after"], facts
    assert facts["after_frames"] == 50, facts


def test_the_first_long_caption_of_a_new_reply_keeps_its_audio_and_played_ms_with_that_reply(server):
    # ``played`` tells the host how much of which reply the line really played, so an interruption cuts the right
    # transcript. A new reply's first caption may be long (a final one carries the whole text): it must still start
    # that reply, or the new audio would count as more of the old one. Old reply, long first caption of a new one,
    # its audio, the next caption of the same reply, then a flush.
    async def scenario():
        host = HostClient(server["url"], TOKEN)
        call_id = await host.open_call(label="Phone", call_id="call-D")
        hub = server["hubs"][call_id]
        closed, captions = [], []
        peer = CallPeer(host, call_id, label="Phone", on_closed=closed.append, on_caption=captions.append)
        await peer.attach()
        try:
            await drain(hub, {"PeerJoined"})
            hub.send_caption("ouroboros", "Old reply.", True, "old")
            hub.play(tone(440, 24000, 24000), "old")                  # 1 s, played to its end
            await asyncio.sleep(0.4)
            old_frames = sum(1 for _ in range(60) if peer.pop_line_frame() is not None)
            hub.send_caption("ouroboros", "я" * 600_000, False, "new")  # 1.2 MB of UTF-8 opens the new reply
            hub.play(tone(440, 24000, 48000), "new")                  # 2 s queued
            await asyncio.sleep(0.5)
            new_frames = sum(1 for _ in range(10) if peer.pop_line_frame() is not None)   # 200 ms played
            hub.send_caption("ouroboros", " and more", False, "new")  # the same reply goes on
            await asyncio.sleep(0.2)
            epoch = hub.flush()                                       # the other party interrupts
            await asyncio.sleep(0.3)
            played = [e.message for e in await drain(hub, {"PeerControl"}) if e.message["type"] == "played"]
            return {"old_frames": old_frames, "new_frames": new_frames, "played": played,
                    "new_captions": [len(c["text"]) for c in captions if c.get("item_id") == "new"],
                    "flushed": (peer.epoch == epoch, peer.pop_line_frame() is None),
                    "closed": list(closed), "connected": peer.connected}
        finally:
            await peer.close("done")

    facts = asyncio.run(scenario())
    old = [p["played_ms"] for p in facts["played"] if p["item_id"] == "old"]
    new = [p["played_ms"] for p in facts["played"] if p["item_id"] == "new"]
    assert facts["closed"] == [] and facts["connected"], facts["closed"]
    assert facts["old_frames"] == 50 and facts["new_frames"] == 10, facts
    assert old and max(old) == 1000, facts["played"]          # nothing of the new reply counted to the old one
    assert new and new[-1] == 200, facts["played"]            # the flush reports what was heard of the new one
    assert facts["new_captions"] == [600_000, len(" and more")] and facts["flushed"] == (True, True), facts

