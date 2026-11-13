"""A scripted SIP peer on asyncio UDP for user-agent tests: it records what the UA sends and answers on cue."""
import asyncio
import secrets
from typing import List, Optional, Tuple

from phone.sip_message import SipMessage, parse, response_to
from phone.sip_ua import SipConfig, SipUA

SDP_ANSWER = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
              "m=audio {port} RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
              "a=ptime:20\r\na=sendrecv\r\n")


class FakeNode(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.received: List[Tuple[SipMessage, Tuple[str, int]]] = []
        self._new = asyncio.Event()

    @classmethod
    async def start(cls) -> "FakeNode":
        loop = asyncio.get_running_loop()
        _, node = await loop.create_datagram_endpoint(cls, local_addr=("127.0.0.1", 0))
        return node

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if not data.strip():
            return
        self.received.append((parse(data), addr))
        self._new.set()

    def close(self):
        self.transport.close()

    def methods(self) -> List[str]:
        return [m.method if m.is_request else str(m.status) for m, _ in self.received]

    def count(self, method: str, *, packets: bool = False) -> int:
        """Requests of ``method``: transactions (distinct branches) by default, every packet with ``packets``."""
        found = [m for m, _ in self.received if m.is_request and m.method == method]
        return len(found) if packets else len({m.top_via.branch for m in found})

    async def expect(self, method: str, timeout: float = 3.0, *, nth: int = 1) -> Tuple[SipMessage, Tuple[str, int]]:
        """The ``nth`` request of ``method`` received so far, waiting up to ``timeout``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = [(m, a) for m, a in self.received if m.is_request and m.method == method]
            if len(found) >= nth:
                return found[nth - 1]
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"no {method} #{nth}; got {self.methods()}")
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def send(self, data, addr):
        self.transport.sendto(data.encode() if isinstance(data, SipMessage) else data, addr)

    def reply(self, request: SipMessage, addr, status: int, *, to_tag: str = "remote1", extra=None,
              body: bytes = b"") -> SipMessage:
        response = response_to(request, status, to_tag=to_tag if status > 100 else "", extra=extra, body=body)
        self.send(response, addr)
        return response

    def request(self, method: str, uri: str, *, call_id: str, from_tag: str, to: str, to_tag: str = "",
                cseq: int = 1, body: bytes = b"", extra=None, branch: Optional[str] = None,
                contact: str = "") -> SipMessage:
        host, port = "127.0.0.1", self.port
        headers = [("Via", f"SIP/2.0/UDP {host}:{port};rport;branch={branch or 'z9hG4bK' + secrets.token_hex(6)}"),
                   ("Max-Forwards", "70"), ("From", f"<sip:+15550001001@{host}>;tag={from_tag}"),
                   ("To", to + (f";tag={to_tag}" if to_tag else "")), ("Call-ID", call_id),
                   ("CSeq", f"{cseq} {method}"), ("Contact", contact or f"<sip:peer@{host}:{port}>")]
        headers += list(extra or [])
        if body:
            headers.append(("Content-Type", "application/sdp"))
        return SipMessage(method=method, uri=uri, headers=headers, body=body)


# -- Putting a SipUA in front of a FakeNode ----------------------------------------------------------------

SDP = SDP_ANSWER.format(port=40000).encode()


async def make_ua(node: FakeNode, **kw) -> SipUA:
    config = dict(registrar="127.0.0.1", registrar_port=node.port, username="line", password="pw",
                  register=False, local_host="127.0.0.1")
    config.update(kw)
    ua = SipUA(SipConfig(**config))
    await ua.start()
    return ua


def contact(node: FakeNode):
    return [("Contact", f"<sip:peer@127.0.0.1:{node.port}>")]


async def answered(ua: SipUA, node: FakeNode, events=None):
    call = await ua.dial("+15550001001", on_event=lambda c, k, d: events.append((k, d)) if events is not None else None)
    invite, addr = await node.expect("INVITE")
    node.reply(invite, addr, 200, extra=contact(node), body=SDP)
    await node.expect("ACK")
    await asyncio.sleep(0.03)
    assert call.state == "answered"
    return call, invite, addr
