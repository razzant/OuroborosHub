"""The phone peer's connection to the Host Service: line facts that survive a full audio queue, and a
socket and tasks that never outlive the call, however the connection ends."""
import asyncio
import base64
import hashlib
import json

import pytest

from conftest import run
from hubfake import LoopbackHubServer, WS_GUID, _ws_encode_server_frame, _ws_read_client_frame
from phone.peer import CallPeer, HostClient, PeerRefused
from phone.ws import WebSocket


def test_bridge_never_drops_line_facts_when_audio_overflows():
    from phone.peer import CallPeer, HostClient, SEND_QUEUE_FRAMES

    async def scenario():
        peer = CallPeer(HostClient("http://127.0.0.1:1", "t"), "c", label="Phone")
        peer.epoch = 0
        peer.send_context("[Phone line notice] The other party pressed key 5.")
        peer._send_control({"type": "played", "item_id": "i", "played_ms": 20})
        for _ in range(SEND_QUEUE_FRAMES + 10):
            peer.push_line_frame(bytes(320))
        return list(peer._control_queue), len(peer._send_queue)

    controls, audio = asyncio.run(scenario())
    assert len(controls) == 2 and "key 5" in controls[0] and audio == SEND_QUEUE_FRAMES


# -- CallPeer.close during connect & WebSocket.connect writer cleanup on timeout ---------------------------

def test_peer_close_during_connect_and_ws_handshake_timeout_close_transport(monkeypatch):
    # Part 1: peer.close() while peer.attach() is inside WebSocket.connect
    async def part1():
        hub = await LoopbackHubServer().start()
        entered, release = hub.add_gate("ws_connect")
        try:
            host = HostClient(hub.url, "tok")
            call_id = await host.open_call(label="Phone", call_id="live-1")
            peer = CallPeer(host, call_id, label="Phone")
            attach_task = asyncio.ensure_future(peer.attach())
            await asyncio.wait_for(entered.wait(), 2.0)
            await peer.close("cancelled")
            release.set()
            with pytest.raises(PeerRefused):
                await asyncio.wait_for(attach_task, 2.0)
            await asyncio.sleep(0.05)
            assert hub.hellos == []
            assert hub.open_ws_writers == set()
            assert peer.closed and peer._ws is None and all(t.done() for t in peer._tasks)
        finally:
            await hub.stop()

    run(part1())

    # Part 2: WebSocket.connect closes StreamWriter when HTTP upgrade times out
    writers = []
    real_open = asyncio.open_connection

    async def spy_open(*args, **kwargs):
        reader, writer = await real_open(*args, **kwargs)
        writers.append(writer)
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", spy_open)

    async def part2():
        accepted = []

        async def silent_server(reader, writer):
            accepted.append(writer)
            await asyncio.sleep(5)

        srv = await asyncio.start_server(silent_server, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            with pytest.raises(asyncio.TimeoutError):
                await WebSocket.connect(f"ws://127.0.0.1:{port}/call/x/peer", timeout=0.1)
            assert len(writers) == 1 and writers[0].is_closing()
        finally:
            for w in accepted:
                w.close()
            srv.close()
            await srv.wait_closed()

    run(part2())


# -- A failed sender closes the peer's socket --------------------------------------------------------------

def test_a_failed_sender_closes_the_socket_without_anyone_calling_close():
    async def scenario():
        hub = await LoopbackHubServer().start()
        try:
            host = HostClient(hub.url, "tok")
            call_id = await host.open_call(label="Phone", call_id="live-1")
            closed = []
            peer = CallPeer(host, call_id, label="Phone", on_closed=closed.append)
            await peer.attach()
            tasks, ws = list(peer._tasks), peer._ws
            assert hub.active_peers and len(tasks) == 2

            async def broken_send(data):
                raise ConnectionResetError("reset by the host")

            ws.send = broken_send
            peer.send_context("[Phone line notice] test")
            for _ in range(100):
                if peer._ws is None and not hub.open_ws_writers:
                    break
                await asyncio.sleep(0.02)
            assert closed == ["send_failed: reset by the host"]
            assert peer.closed and peer._ws is None and ws.closed and all(t.done() for t in tasks)
            assert hub.open_ws_writers == set() and hub.active_peers == set()  # the host saw the socket close
            await peer.close("done")  # still harmless afterwards
        finally:
            await hub.stop()

    run(scenario())


def test_standalone_peer_closed_by_server_state_cleans_up_sender_and_ws():
    async def scenario():
        server_writers = []

        async def state_close_server(reader, writer):
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip()
                       for l in lines[1:] if ":" in l}
            if method == "POST":
                length = int(headers.get("content-length", "0"))
                if length:
                    await reader.readexactly(length)
                body = b'{"call_id":"c1","peer_path":"/call/c1/peer"}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
                writer.close()
                return
            server_writers.append(writer)
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + WS_GUID).encode()).digest()).decode()
            writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
            await writer.drain()
            await _ws_read_client_frame(reader)  # hello
            writer.write(_ws_encode_server_frame(0x1, json.dumps({
                "type": "flush", "epoch": 0
            }, separators=(",", ":")).encode()))
            await writer.drain()
            await asyncio.sleep(0.03)
            writer.write(_ws_encode_server_frame(0x1, json.dumps({
                "type": "state", "state": "closed", "reason": "finished"
            }, separators=(",", ":")).encode()))
            await writer.drain()
            await asyncio.sleep(1.0)
            writer.close()

        srv = await asyncio.start_server(state_close_server, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            host = HostClient(f"http://127.0.0.1:{port}", "tok")
            cid = await host.open_call(label="Phone", call_id="c1")
            peer = CallPeer(host, cid, label="Phone")
            await peer.attach()
            tasks, ws = list(peer._tasks), peer._ws
            for _ in range(50):
                if peer.closed and peer._ws is None:
                    break
                await asyncio.sleep(0.02)
            # Nobody calls close(): the call's own end must leave no socket and no task behind.
            assert peer.closed and peer.closed_reason == "finished"
            assert peer._ws is None and ws.closed and all(t.done() for t in tasks)
            await peer.close("done")
        finally:
            for w in server_writers:
                w.close()
            srv.close()
            await srv.wait_closed()

    run(scenario())


# -- Text beside the audio: read whole; only the audio has a frame bound -----------------------------------

def test_text_beside_the_audio_is_read_whole_and_only_audio_has_a_frame_bound():
    import struct
    from phone.ws import OP_BINARY, OP_CONT, OP_TEXT, WebSocketClosed

    def frame(opcode, payload, fin=True):
        head = bytearray([(0x80 if fin else 0) | opcode])
        if len(payload) < 126:
            head.append(len(payload))
        else:
            head += bytes([126]) + struct.pack("!H", len(payload))
        return bytes(head) + payload

    async def scenario():
        async def serve(reader, writer):
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n")
                       if line.lower().startswith("sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
            writer.write(frame(OP_TEXT, b"a" * 40, fin=False) + frame(OP_CONT, b"b" * 40, fin=False)
                         + frame(OP_CONT, b"c" * 40))          # 120 bytes in three frames: past max_size 64
            writer.write(frame(OP_BINARY, b"\x01" * 64))        # audio within its frame bound
            writer.write(frame(OP_TEXT, b"x" * 500))            # one text frame far past max_size
            writer.write(frame(OP_TEXT, b"short"))
            writer.write(frame(OP_BINARY, b"\x02" * 66))        # audio past its bound closes the socket
            await writer.drain()
            await asyncio.sleep(2)
            writer.close()

        srv = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            ws = await WebSocket.connect(f"ws://127.0.0.1:{port}/call/x/peer", max_size=64)
            got = [await ws.recv() for _ in range(4)]
            with pytest.raises(WebSocketClosed) as closed:
                await ws.recv()
            return got, closed.value.code
        finally:
            srv.close()

    got, code = run(scenario())
    assert got == ["a" * 40 + "b" * 40 + "c" * 40, b"\x01" * 64, "x" * 500, "short"] and code == 1009, (got, code)


# -- The Host Service client: loopback only, no proxy and no redirect --------------------------------------

TOKEN_SENTINEL = "sentinel-token-not-a-secret"


@pytest.fixture()
def http_sentinels():
    """Plain HTTP servers that record what reaches them: (start(name, answer) -> url, seen)."""
    import http.server
    import threading

    seen, servers = {}, []

    def start(name, answer):
        seen[name] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _any(self):
                length = int(self.headers.get("content-length") or 0)
                seen[name].append((self.command, self.path, self.headers.get("x-skill-token"),
                                   self.rfile.read(length) if length else b""))
                status, headers, body = answer
                self.send_response(status)
                for header in headers:
                    self.send_header(*header)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _any

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start, seen
    for server in servers:
        server.shutdown()
        server.server_close()


def call_opened(call_id):
    return 200, [("content-type", "application/json")], json.dumps({"call_id": call_id}).encode()


def test_the_host_client_never_goes_through_a_proxy(monkeypatch, http_sentinels):
    # A proxy in the environment (macOS urllib also reads the system settings, whose exceptions leave 127.0.0.1
    # out): the token and the call's event would go there. A default opener built now would read it.
    import urllib.request

    start, seen = http_sentinels
    proxy_url, host_url = start("proxy", call_opened("via-proxy")), start("host", call_opened("c1"))
    monkeypatch.setattr(urllib.request, "_opener", None)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    call_id = run(HostClient(host_url, TOKEN_SENTINEL).open_call(label="Phone", call_id="c1"))
    assert seen["proxy"] == [] and call_id == "c1", seen
    assert [(m, p, t) for m, p, t, _ in seen["host"]] == [("POST", "/call/open", TOKEN_SENTINEL)]


def test_the_host_client_never_follows_a_redirect(monkeypatch, http_sentinels):
    import urllib.request

    start, seen = http_sentinels
    elsewhere_url = start("elsewhere", call_opened("stolen"))
    redirecting_url = start("redirecting", (302, [("location", elsewhere_url + "/steal")], b""))
    monkeypatch.setattr(urllib.request, "_opener", None)
    for name in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    try:
        outcome = run(HostClient(redirecting_url, TOKEN_SENTINEL).open_call(label="Phone", call_id="c1"))
    except PeerRefused as exc:
        outcome = (exc.code, exc.status)
    assert seen["elsewhere"] == [] and outcome == ("http_302", 302), (outcome, seen)
    assert len(seen["redirecting"]) == 1
