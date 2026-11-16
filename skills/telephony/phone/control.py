"""Private control channel between the phone tools and the line companion.

The tools run inside the turn that called them (a worker or a short-lived
extension child); the companion runs under the host's supervisor and holds the
SIP and RTP sockets. They meet on loopback TCP: the companion binds 127.0.0.1
on a free port and publishes ``control.json`` (port, random token, pid) in the
skill's state directory with owner-only permissions; a tool reads it, connects,
sends one JSON request line with the token and reads one JSON reply line.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import pathlib
import secrets
import socket
from typing import Any, Awaitable, Callable, Dict, Optional, Set

CONTROL_FILE = "control.json"
MAX_MESSAGE_BYTES = 256 * 1024
CONNECT_TIMEOUT_SEC = 3.0
STOP_WAIT_SEC = 1.0
STOPPING_REPLY = {"ok": False, "code": "PHONE_LINE_STOPPING",
                  "message": "The phone line companion is stopping (a restart or an update); any call on the line "
                             "was ended. Try again in a moment."}

Handler = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class ControlUnavailable(RuntimeError):
    """The companion is not running or not answering; the text says why in plain words."""

    def __init__(self, message: str, *, reason: str = "unavailable"):
        super().__init__(message)
        self.reason = reason


def _write_private(path: pathlib.Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.replace(str(tmp), str(path))


def read_endpoint(state_dir: pathlib.Path) -> Dict[str, Any]:
    path = pathlib.Path(state_dir) / CONTROL_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ControlUnavailable("the phone line companion is not running (the skill may be disabled, "
                                 "still starting, or not configured)", reason="not_running") from None
    except (OSError, ValueError):
        raise ControlUnavailable("the phone line companion's control file is unreadable",
                                 reason="unreadable") from None
    port = data.get("port") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or isinstance(port, bool) or not isinstance(port, int)
            or not 1 <= port <= 65535 or not data.get("token")):
        raise ControlUnavailable("the phone line companion's control file is unreadable", reason="unreadable")
    return data


def request(state_dir: pathlib.Path, op: str, args: Optional[Dict[str, Any]] = None,
            *, timeout_sec: float = 30.0) -> Dict[str, Any]:
    endpoint = read_endpoint(state_dir)
    line = json.dumps({"token": endpoint["token"], "op": op, "args": dict(args or {})}).encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=CONNECT_TIMEOUT_SEC) as sock:
            sock.settimeout(timeout_sec)
            sock.sendall(line + b"\n")
            chunks = bytearray()
            while not chunks.endswith(b"\n"):
                piece = sock.recv(65536)
                if not piece:
                    break
                chunks.extend(piece)
                if len(chunks) > MAX_MESSAGE_BYTES:
                    raise ControlUnavailable("the phone line companion sent an oversized reply", reason="oversized")
    except ControlUnavailable:
        raise
    except ConnectionRefusedError:
        raise ControlUnavailable("the phone line companion is not running (the skill may be disabled, "
                                 "still starting, or restarting)", reason="not_running") from None
    except socket.timeout:
        raise ControlUnavailable("the phone line companion is not answering (it may be busy or restarting); "
                                 "try again shortly", reason="timeout") from None
    except OSError:
        raise ControlUnavailable("the phone line companion is not answering (it may be restarting); "
                                 "try again shortly", reason="unavailable") from None
    try:
        reply = json.loads(bytes(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ControlUnavailable("the phone line companion sent an unreadable reply", reason="bad_reply") from None
    if not isinstance(reply, dict):
        raise ControlUnavailable("the phone line companion sent an unreadable reply", reason="bad_reply")
    return reply


class ControlServer:
    """The companion's end, on its own asyncio loop."""

    def __init__(self, state_dir: pathlib.Path, handler: Handler):
        self.state_dir = pathlib.Path(state_dir)
        self._handler = handler
        self._token = secrets.token_urlsafe(32)
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Set[asyncio.Task] = set()

    async def start(self) -> int:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=MAX_MESSAGE_BYTES)
        port = self._server.sockets[0].getsockname()[1]
        _write_private(self.state_dir / CONTROL_FILE,
                       json.dumps({"port": port, "token": self._token, "pid": os.getpid()}).encode("utf-8"))
        return port

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            raw = await asyncio.wait_for(reader.readline(), 30)
            reply = await self._answer(raw)
            writer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except asyncio.CancelledError:
            try:  # the companion is stopping: a plain answer instead of a dropped connection
                writer.write(json.dumps(STOPPING_REPLY).encode("utf-8") + b"\n")
            except Exception:
                pass
            raise
        except (asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            self._clients.discard(task)
            writer.close()

    async def _answer(self, raw: bytes) -> Dict[str, Any]:
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"ok": False, "code": "PHONE_CONTROL_REFUSED", "message": "unreadable request"}
        if not isinstance(message, dict) or not hmac.compare_digest(str(message.get("token") or ""), self._token):
            return {"ok": False, "code": "PHONE_CONTROL_REFUSED", "message": "not authorized"}
        args = message.get("args") if isinstance(message.get("args"), dict) else {}
        try:
            return await self._handler(str(message.get("op") or ""), args)
        except Exception as exc:  # the reply is this transport's error channel
            return {"ok": False, "code": "PHONE_COMPANION_ERROR",
                    "message": f"the phone line companion failed: {type(exc).__name__}: {exc}"}

    def close(self) -> None:
        """Take no new requests; the ones in flight go on."""
        if self._server is not None:
            self._server.close()

    async def stop(self) -> None:
        """Close the channel within ``STOP_WAIT_SEC`` without waiting for the requests still in flight: they are
        cancelled and answered ``PHONE_LINE_STOPPING`` (on Python 3.12 ``wait_closed`` alone would wait for them to
        finish)."""
        self.close()
        loop = asyncio.get_running_loop()
        until = loop.time() + STOP_WAIT_SEC  # one budget for both waits
        current = asyncio.current_task()
        clients = [task for task in self._clients if task is not current and not task.done()]
        for task in clients:
            task.cancel()
        if clients:
            await asyncio.wait(clients, timeout=STOP_WAIT_SEC)
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), max(until - loop.time(), 0.05))
            except asyncio.TimeoutError:
                pass
        try:
            current = json.loads((self.state_dir / CONTROL_FILE).read_text(encoding="utf-8"))
            if isinstance(current, dict) and current.get("token") == self._token:
                (self.state_dir / CONTROL_FILE).unlink()
        except (OSError, ValueError):
            pass
