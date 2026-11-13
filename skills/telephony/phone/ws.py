"""A minimal RFC 6455 WebSocket client on asyncio streams (no third-party package).

The companion talks only to the Host Service on loopback (``ws://127.0.0.1``),
so plain TCP is enough; ``wss`` is accepted through ``ssl`` for completeness.
Using the standard library keeps the skill free of the ``websockets`` package,
whose client API changed between the versions Ouroboros forks may ship
(``extra_headers`` became ``additional_headers``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from typing import Dict, Optional, Tuple, Union
from urllib.parse import urlsplit

from .sip_message import ascii_int

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 §1.3
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(ConnectionError):
    def __init__(self, code: int = 1006, reason: str = ""):
        super().__init__(f"websocket closed {code} {reason}".strip())
        self.code, self.reason = code, reason


class WebSocketHandshakeError(ConnectionError):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"websocket handshake refused: HTTP {status} {body}".strip())
        self.status, self.body = status, body


class WebSocket:
    """``max_size`` bounds a binary message (an audio frame); past it the socket closes (1009). A text message is
    read whole: this client talks only to the local Host Service, whose captions have no byte cutoff."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, max_size: int):
        self._reader, self._writer = reader, writer
        self.max_size = max_size
        self.closed = False
        self.close_code: Optional[int] = None
        self.close_reason = ""
        self._send_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, url: str, headers: Optional[Dict[str, str]] = None, *, timeout: float = 10.0,
                      max_size: int = 1 << 20) -> "WebSocket":
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise ValueError(f"not a websocket URL: {url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        context = ssl.create_default_context() if parts.scheme == "wss" else None
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=context), timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            for name, value in (headers or {}).items():
                if "\r" in name + value or "\n" in name + value:
                    raise ValueError("header injection")
                lines.append(f"{name}: {value}")
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            await asyncio.wait_for(writer.drain(), timeout)
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
                raise WebSocketHandshakeError(0, "no handshake answer") from exc
            status_line, *header_lines = head.decode("latin-1").split("\r\n")
            status_parts = status_line.split(" ")
            status = ascii_int(status_parts[1], 0) if len(status_parts) > 1 else 0
            answer = {}
            for line in header_lines:
                name, _, value = line.partition(":")
                if name:
                    answer[name.strip().lower()] = value.strip()
            if status != 101:
                body = ""
                try:
                    body = (await asyncio.wait_for(reader.read(512), 1.0)).decode("utf-8", "replace")
                except (asyncio.TimeoutError, ConnectionError, OSError):
                    pass
                raise WebSocketHandshakeError(status, body)
            expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
            if answer.get("sec-websocket-accept") != expected:
                raise WebSocketHandshakeError(status, "bad Sec-WebSocket-Accept")
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer, max_size=max_size)

    async def _write_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = _mask(payload, mask)
        async with self._send_lock:
            if self.closed and opcode != OP_CLOSE:
                raise WebSocketClosed(self.close_code or 1006, self.close_reason)
            self._writer.write(bytes(header) + masked)
            await self._writer.drain()

    async def send(self, data: Union[str, bytes]) -> None:
        if isinstance(data, str):
            await self._write_frame(OP_TEXT, data.encode("utf-8"))
        else:
            await self._write_frame(OP_BINARY, bytes(data))

    async def _read_frame(self, text: bool) -> Tuple[bool, int, bytes]:
        """One frame; ``text``: a continuation frame belongs to a text message, which no size bound applies to."""
        first, second = await self._reader.readexactly(2)
        fin, opcode = bool(first & 0x80), first & 0x0F
        masked, length = bool(second & 0x80), second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", await self._reader.readexactly(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", await self._reader.readexactly(8))
        if length > self.max_size and not (opcode == OP_TEXT or opcode == OP_CONT and text):
            raise WebSocketClosed(1009, "message too big")
        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length)
        if masked:
            payload = _mask(payload, mask)
        return fin, opcode, payload

    async def recv(self) -> Union[str, bytes]:
        """The next text (str) or binary (bytes) message; raises ``WebSocketClosed`` at the end."""
        message_opcode, chunks = None, []
        while True:
            try:
                fin, opcode, payload = await self._read_frame(message_opcode == OP_TEXT)
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                self.closed = True
                self._writer.close()
                if isinstance(exc, WebSocketClosed):
                    raise
                raise WebSocketClosed(self.close_code or 1006, self.close_reason) from None
            if opcode == OP_PING:
                try:
                    await self._write_frame(OP_PONG, payload)
                except (ConnectionError, OSError, WebSocketClosed):
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                self.close_code, self.close_reason = code, reason
                if not self.closed:
                    try:
                        await self._write_frame(OP_CLOSE, payload[:2])
                    except (ConnectionError, OSError, WebSocketClosed):
                        pass
                self.closed = True
                self._writer.close()
                raise WebSocketClosed(code, reason)
            if opcode in (OP_TEXT, OP_BINARY):
                message_opcode, chunks = opcode, [payload]
            elif opcode == OP_CONT and message_opcode is not None:
                chunks.append(payload)
            else:
                continue
            if fin:
                data = b"".join(chunks)
                if message_opcode == OP_BINARY and len(data) > self.max_size:
                    self.closed = True
                    self._writer.close()
                    raise WebSocketClosed(1009, "message too big")
                if message_opcode == OP_TEXT:
                    try:
                        return data.decode("utf-8")
                    except UnicodeDecodeError:
                        self.closed = True
                        self._writer.close()
                        raise WebSocketClosed(1007, "invalid UTF-8") from None
                return data

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            self._writer.close()
            return
        self.closed = True
        try:
            await asyncio.wait_for(self._write_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:120]), 2)
        except (asyncio.TimeoutError, ConnectionError, OSError, WebSocketClosed, RuntimeError):
            pass
        finally:
            self._writer.close()


def _mask(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return b""
    # Whole-int XOR is far faster than a per-byte loop for 1-2 KiB audio frames.
    repeated = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(repeated, "little")).to_bytes(len(payload), "little")
