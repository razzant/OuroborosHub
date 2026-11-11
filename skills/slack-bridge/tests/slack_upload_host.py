"""A real local upload host and a scripted Slack Web API for External Upload tests.

``UploadHost`` is an HTTP/1.1 server on 127.0.0.1 with small socket buffers: each
connection is served by its own thread, which reads the body at a planned rate,
so a fast client really waits on socket backpressure. A slot's plan can drop the
connection after some body bytes or before answering, delay the answer, dribble
it byte by byte, or answer another status or body. The host records what each
slot received: declared Content-Length, any Transfer-Encoding, byte count, SHA-256
and a timeline of received counts.

``FakeSlack`` answers files.getUploadURLExternal with slots on that host and
files.completeUploadExternal by recording the shared file, the provider effect a
duplicate would repeat. Scripted answers can replace either call. Nothing here
contacts Slack; it imports no bridge code, so old and new bridges share it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl

import httpx


@dataclass
class SlotPlan:
    read_rate: float | None = None  # body bytes per second; None reads as fast as possible
    close_after: int | None = None  # reset the connection after this many body bytes
    close_before_answer: bool = False  # read the whole body, then close without answering
    answer_delay: float = 0.0  # seconds between the last body byte and the answer
    trickle: float = 0.0  # seconds between the answer's body bytes
    status: int = 200
    body: str | None = None  # default: "OK - <bytes received>"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SlotRecord:
    content_length: int = -1
    transfer_encoding: str | None = None
    received: int = 0
    sha256: str = ""
    complete: bool = False
    answered: bool = False
    timeline: list[tuple[float, int]] = field(default_factory=list)


class UploadHost:
    def __init__(self, *, recv_buffer: int = 65536, read_chunk: int = 16384) -> None:
        self.plans: dict[str, SlotPlan] = {}
        self.records: dict[str, SlotRecord] = {}
        self._read_chunk = read_chunk
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Set before listen() so every accepted socket inherits the small window.
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recv_buffer)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self.port = self._server.getsockname()[1]
        self._stopped = threading.Event()
        self._threads: list[threading.Thread] = []
        self._connections: list[socket.socket] = []
        self._accepter = threading.Thread(target=self._accept, name="upload-host", daemon=True)
        self._accepter.start()

    def url(self, slot: str) -> str:
        return f"http://127.0.0.1:{self.port}/upload/{slot}"

    def plan(self, slot: str, **plan: Any) -> None:
        self.plans[slot] = SlotPlan(**plan)

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _address = self._server.accept()
            except OSError:
                return
            self._connections.append(connection)
            thread = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            self._threads.append(thread)
            thread.start()

    @staticmethod
    def _reset(connection: socket.socket) -> None:
        # SO_LINGER 0: close() sends RST, so the client's next write or read fails at once.
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()

    def _serve(self, connection: socket.socket) -> None:
        try:
            self._serve_one(connection)
        except OSError:
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _serve_one(self, connection: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            piece = connection.recv(4096)
            if not piece:
                return
            head += piece
        head, _separator, body = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        path = lines[0].split(" ")[1]
        headers = {}
        for line in lines[1:]:
            name, _colon, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        slot = path.rsplit("/", 1)[-1]
        plan = self.plans.get(slot, SlotPlan())
        record = SlotRecord(content_length=int(headers.get("content-length", "-1")),
                            transfer_encoding=headers.get("transfer-encoding"))
        self.records[slot] = record
        digest = hashlib.sha256(body)
        record.received = len(body)
        record.timeline.append((time.monotonic(), record.received))
        while record.received < record.content_length:
            if plan.close_after is not None and record.received >= plan.close_after:
                self._reset(connection)
                return
            want = min(self._read_chunk, record.content_length - record.received)
            if plan.close_after is not None:
                want = min(want, plan.close_after - record.received)
            piece = connection.recv(want)
            if not piece:
                return
            digest.update(piece)
            record.received += len(piece)
            record.timeline.append((time.monotonic(), record.received))
            if plan.read_rate:
                time.sleep(len(piece) / plan.read_rate)
        record.sha256 = digest.hexdigest()
        record.complete = record.received == record.content_length
        if plan.close_before_answer:
            connection.close()
            return
        if plan.answer_delay:
            time.sleep(plan.answer_delay)
        answer = (plan.body if plan.body is not None else f"OK - {record.received}").encode("ascii")
        extra = "".join(f"{name}: {value}\r\n" for name, value in plan.headers.items())
        status = (f"HTTP/1.1 {plan.status} Answer\r\nContent-Type: text/plain\r\n"
                  f"Content-Length: {len(answer)}\r\nConnection: close\r\n{extra}\r\n").encode("ascii")
        if plan.trickle:
            connection.sendall(status)
            for index in range(len(answer)):
                time.sleep(plan.trickle)
                connection.sendall(answer[index:index + 1])
        else:
            connection.sendall(status + answer)
        record.answered = True

    def close(self) -> None:
        self._stopped.set()
        self._server.close()
        for connection in self._connections:
            try:
                connection.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=5)


Answer = Callable[[httpx.Request], Any]


class FakeSlack:
    """files.getUploadURLExternal and files.completeUploadExternal for the real SlackClient.

    ``slot_answers`` / ``complete_answers`` are consumed one per call; ``None`` (or an
    exhausted list) gives the ordinary answer. ``published`` holds each file whose
    completion the provider applied, including one whose answer was then lost.
    """

    def __init__(self, host: Any, *, slot_answers: list[Answer | None] | None = None,
                 complete_answers: list[Answer | None] | None = None, complete_delay: float = 0.0,
                 prefix: str = "F") -> None:
        self.host = host
        self.prefix = prefix
        self.slot_answers = list(slot_answers or [])
        self.complete_answers = list(complete_answers or [])
        self.complete_delay = complete_delay
        self.slots: list[str] = []
        self.declared: dict[str, int] = {}
        self.slot_requests = 0
        self.completion_requests: list[dict[str, Any]] = []
        self.published: list[str] = []
        self.other: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "files.getUploadURLExternal":
            self.slot_requests += 1
            answer = self.slot_answers.pop(0) if self.slot_answers else None
            if answer is not None:
                return answer(request) if callable(answer) else answer
            file_id = f"{self.prefix}{len(self.slots) + 1}"
            self.slots.append(file_id)
            self.declared[file_id] = int(dict(parse_qsl(request.content.decode("utf-8")))["length"])
            return httpx.Response(200, json={"ok": True, "file_id": file_id, "upload_url": self.host.url(file_id)})
        if method == "files.completeUploadExternal":
            payload = json.loads(request.content)
            self.completion_requests.append(payload)
            if self.complete_delay:
                await asyncio.sleep(self.complete_delay)
            answer = self.complete_answers.pop(0) if self.complete_answers else None
            if answer is not None:
                return answer(request) if callable(answer) else answer
            file_id = payload["files"][0]["id"]
            self.published.append(file_id)
            return httpx.Response(200, json={"ok": True, "files": [{"id": file_id, "title": payload["files"][0]["title"]}]})
        self.other.append(method)
        raise AssertionError(f"unexpected Slack method {method}")

    def shared_then_lost(self, error: type[Exception]) -> Answer:
        """Slack applies the completion; only its answer to this worker is lost."""
        def answer(request: httpx.Request) -> httpx.Response:
            self.published.append(json.loads(request.content)["files"][0]["id"])
            raise error("completion answer lost", request=request) if issubclass(error, httpx.RequestError) \
                else error("completion answer lost")
        return answer


def http_client(slack: FakeSlack, *, timeout: float = 30.0, send_buffer: int = 65536) -> httpx.AsyncClient:
    """The client SlackClient would own, routing slack.com to ``slack`` and uploads to the real host."""
    real = httpx.AsyncHTTPTransport(socket_options=[(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer)])
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=False, follow_redirects=True,
                             mounts={"https://slack.com": httpx.MockTransport(slack), "all://": real})
