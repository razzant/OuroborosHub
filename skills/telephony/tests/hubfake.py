"""A stand-in Host Service and scripted legs/providers for the call-manager and daemon tests: a real HTTP +
WebSocket server on loopback that answers /call/open and /call/{id}/peer (with pause gates to stop a
request at an await point), a provider and legs that do what the test tells them, and the scenes the
manager tests build on them."""
import asyncio
import base64
import hashlib
import json
import struct

from conftest import until
from phone import config as cfg
from phone import manager as mgr
from phone.peer import HostClient
from phone.provider import Leg, TelephonyProvider
from sipfake import FakeNode


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_encode_server_frame(opcode: int, payload: bytes) -> bytes:
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head.extend(struct.pack("!H", n))
    else:
        head.append(127)
        head.extend(struct.pack("!Q", n))
    return bytes(head) + payload


async def _ws_read_client_frame(reader: asyncio.StreamReader):
    first = await reader.readexactly(2)
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b"\x00\x00\x00\x00"
    raw = await reader.readexactly(length)
    data = bytes(b ^ mask[i & 3] for i, b in enumerate(raw)) if masked else raw
    return opcode, data


class LoopbackHubServer:
    """Minimal real TCP HTTP + RFC 6455 server matching /call/open and /call/{id}/peer with pause gates."""

    def __init__(self):
        self._server = None
        self.port = 0
        self.open_requests = []
        self.hellos = []
        self.byes = []
        self.contexts = []
        self.active_peers = set()
        self.open_ws_writers = set()
        self._gates = {}
        self._entered = {}
        self._seq = 0

    def add_gate(self, name: str):
        entered = asyncio.Event()
        release = asyncio.Event()
        self._entered[name] = entered
        self._gates[name] = release
        return entered, release

    async def _pause(self, name: str):
        if name in self._entered:
            self._entered[name].set()
            await self._gates[name].wait()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        for gate in self._gates.values():
            gate.set()
        for writer in list(self.open_ws_writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            raw_head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = raw_head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if method == "POST" and path == "/call/open":
            length = int(headers.get("content-length", "0"))
            body = json.loads((await reader.readexactly(length)).decode("utf-8")) if length else {}
            self.open_requests.append(body)
            await self._pause("open_call")
            self._seq += 1
            call_id = body.get("call_id") or f"presence-{self._seq:04d}"
            payload = json.dumps({"call_id": call_id, "peer_path": f"/call/{call_id}/peer"}).encode("utf-8")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii") + payload)
            await writer.drain()
            writer.close()
            return

        if method == "GET" and path.startswith("/call/") and path.endswith("/peer"):
            self.open_ws_writers.add(writer)
            peer_id = f"peer-{len(self.hellos) + len(self.open_ws_writers)}"
            joined = False
            try:
                await self._pause("ws_connect")
                accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + WS_GUID).encode()).digest()).decode()
                writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii"))
                await writer.drain()
                while True:
                    opcode, data = await _ws_read_client_frame(reader)
                    if opcode == 0x8:
                        writer.write(_ws_encode_server_frame(0x8, b"\x03\xe8"))
                        await writer.drain()
                        break
                    if opcode == 0x9:
                        writer.write(_ws_encode_server_frame(0xA, data))
                        await writer.drain()
                        continue
                    if opcode == 0x1:
                        msg = json.loads(data.decode("utf-8"))
                        kind = msg.get("type")
                        if kind == "hello":
                            self.hellos.append(msg)
                            self.active_peers.add(peer_id)
                            joined = True
                            await self._pause("ws_hello")
                            welcome = json.dumps({"type": "flush", "epoch": 0}, separators=(",", ":"))
                            writer.write(_ws_encode_server_frame(0x1, welcome.encode("utf-8")))
                            await writer.drain()
                        elif kind == "context":
                            self.contexts.append(msg)
                        elif kind == "bye":
                            self.byes.append(msg)
                            break
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass
            finally:
                if joined:
                    self.active_peers.discard(peer_id)
                self.open_ws_writers.discard(writer)
                writer.close()
            return

        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        writer.close()


class ScriptedLeg(Leg):
    def __init__(self, direction="out", number="+15550001001", accept_gate=None):
        self.id = f"leg-{id(self)}"
        self.direction, self.remote_number = direction, number
        self.state = "incoming" if direction == "in" else "calling"
        self.end_reason, self.end_status = "", 0
        self.on_event = lambda leg, kind, data: None
        self.hangups, self.rejected = [], []
        self._accept_gate = accept_gate
        self.hangup_delay = 0.0

    def read_frame(self):
        return bytes(320)

    def write_frame(self, pcm):
        pass

    async def send_dtmf(self, digits):
        return "rfc4733"

    def end(self, reason, status=0):
        if self.state == "ended":
            return
        self.state, self.end_reason, self.end_status = "ended", reason, status
        self.on_event(self, "ended", {"reason": reason, "status": status})

    async def hangup(self, reason="local_hangup"):
        self.hangups.append(reason)
        if self.hangup_delay:
            await asyncio.sleep(self.hangup_delay)
        self.end(reason)

    def ringing(self):
        if self.state == "incoming":
            self.state = "ringing"

    async def accept(self):
        if self._accept_gate is not None:
            entered, release = self._accept_gate
            entered.set()
            await release.wait()
        if self.state == "ended":
            return
        self.state = "answered"
        self.on_event(self, "answered", {})

    async def reject(self, status=486, reason=""):
        self.rejected.append(status)
        self.end(reason or f"rejected_{status}", status)


class ScriptedProvider(TelephonyProvider):
    def __init__(self, dial_gate=None):
        super().__init__(lambda leg: None, lambda status: None)
        self.legs = []
        self._dial_gate = dial_gate

    async def start(self):
        pass

    async def stop(self, deadline=None):
        pass

    async def dial(self, number, on_event):
        if self._dial_gate is not None:
            entered, release = self._dial_gate
            entered.set()
            await release.wait()
        leg = ScriptedLeg("out", number)
        leg.on_event = on_event
        self.legs.append(leg)
        return leg

    def bind_events(self, leg, on_event):
        leg.on_event = on_event

    def status(self):
        return {"registration": "registered"}

    def account_id(self):
        return "line@pbx.test"


def history(state_dir):
    path = state_dir / mgr.HISTORY_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


ANSWERED = "answered"


def contexts(hub, text):
    return [c["text"] for c in hub.contexts if text in c.get("text", "")]


def sip_line_config(node: FakeNode):
    return cfg._merge(cfg.DEFAULTS, {
        "sip": {"registrar": "127.0.0.1", "port": node.port, "username": "line", "register": False,
                "local_host": "127.0.0.1"},
        "outbound": {"allowed_prefixes": ["+1555"]}})


def scripted_manager(tmp_path, hub, provider=None, **extra):
    config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}, "inbound": {"policy": "all"},
                                       **extra})
    m = mgr.CallManager(tmp_path, config, "tok", HostClient(hub.url, "tok"), provider=provider or ScriptedProvider())
    m.policy.limits.dial_cooldown_seconds = 0
    return m, config


async def answered_place_call(m, provider, hub, number="+15550001001"):
    result = await m.dial(number=number, purpose="p", owner_approved=True)
    assert result["ok"] is True, result
    leg = provider.legs[-1]
    leg.state = "answered"
    leg.on_event(leg, "answered", {})
    assert await until(lambda: contexts(hub, ANSWERED)), hub.contexts
    return m.active, leg
