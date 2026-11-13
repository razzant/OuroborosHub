"""The phone line as a ``meeting`` peer of a live call on the Host Service.

``HostClient.open_call`` is ``POST /call/open`` (attach to an existing call by
id, or ask place admission for a new Presence call with an event).
``CallPeer`` holds ``WS /call/{id}/peer``:

* line -> call: each 20 ms line frame (8 kHz) is upsampled to the declared
  rate with an anti-aliasing filter and sent as an AUDIO_IN frame stamped with
  the hub's current epoch; a bounded send queue drops the oldest audio rather
  than ever blocking the media clock;
* call -> line: the hub sends a whole answer far faster than real time; frames
  of the current epoch are downsampled to 8 kHz and queued per caption item;
  ``pop_line_frame`` hands out exactly one 20 ms frame per tick and counts what
  was really played, reported back as ``played`` (every ~100 ms and at each
  item boundary) so an interruption cuts the transcript where the line
  stopped; ``flush`` drops everything queued at once;
* captions become the call transcript; ``state: closed`` ends the peer.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from . import callproto as cp
from .dsp import Downsampler, Upsampler
from .jitter import FRAME_BYTES
from .ws import WebSocket, WebSocketClosed, WebSocketHandshakeError

log = logging.getLogger("telephony.peer")

HTTP_TIMEOUT = 20.0
ATTACH_TIMEOUT = 10.0
SEND_QUEUE_FRAMES = 25            # 0.5 s of line audio toward the hub
OUTPUT_LIMIT_FRAMES = 15_000      # 5 minutes of queued speech toward the line
PLAYED_REPORT_MS = 100


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):  # never follow a redirect off loopback
        return None


# The Host Service is on loopback: no proxy (macOS urllib also reads the system proxy settings, whose default
# exceptions leave 127.0.0.1 out) and no redirect may carry the skill token or a call's event anywhere else.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class PeerRefused(RuntimeError):
    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(message or code)
        self.code, self.message, self.status = code, message or code, status


class HostClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _post(self, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"content-type": "application/json", "x-skill-token": self.token})
        try:
            with _OPENER.open(request, timeout=HTTP_TIMEOUT) as response:
                payload = json.loads(response.read(cp.MAX_CONTROL_BYTES) or b"{}")
                return response.status, payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(cp.MAX_CONTROL_BYTES) or b"{}")
            except ValueError:
                payload = {}
            return exc.code, payload if isinstance(payload, dict) else {}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PeerRefused("host_unreachable", f"the Host Service did not answer: {exc}") from None

    async def open_call(self, *, label: str, call_id: str = "", event: Optional[Dict[str, Any]] = None,
                        binding_id: str = "") -> str:
        body: Dict[str, Any] = {"role": cp.PEER_MEETING, "mic": True, "speaker": True, "label": label[:120]}
        if call_id:
            body["call_id"] = call_id
        if event is not None:
            body["event"] = event
        if binding_id:
            body["binding_id"] = binding_id
        status, payload = await asyncio.to_thread(self._post, "/call/open", body)
        if status != 200 or not isinstance(payload.get("call_id"), str):
            raise PeerRefused(str(payload.get("code") or f"http_{status}"), str(payload.get("error") or ""), status)
        return payload["call_id"]

    def ws_url(self, call_id: str) -> str:
        base = self.url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/call/{call_id}/peer"


class _Item:
    __slots__ = ("item_id", "played_samples", "reported_ms")

    def __init__(self, item_id: str):
        self.item_id = item_id
        self.played_samples = 0
        self.reported_ms = -1


class CallPeer:
    def __init__(self, host: HostClient, call_id: str, *, label: str, rate: int = 24000,
                 on_closed: Callable[[str], None] = lambda reason: None,
                 on_caption: Callable[[Dict[str, Any]], None] = lambda caption: None,
                 on_state: Callable[[str], None] = lambda state: None):
        if rate % 8000:
            raise ValueError("the peer rate must be a multiple of 8000")
        self.host, self.call_id, self.label, self.rate = host, call_id, label, rate
        self.factor = rate // 8000
        self.on_closed, self.on_caption, self.on_state = on_closed, on_caption, on_state
        self.epoch: Optional[int] = None
        self.state = ""
        self.closed_reason = ""
        self.hold_output = False  # ringing: keep the agent's words until someone answers
        self._ws: Optional[WebSocket] = None
        self._up = Upsampler(self.factor)
        self._down = Downsampler(self.factor)
        self._seq = 0
        self._send_queue: Deque[bytes] = collections.deque()      # audio: bounded, the oldest is dropped
        self._control_queue: Deque[str] = collections.deque()    # context/played/bye: never dropped
        self._send_wake = asyncio.Event()
        self._output: Deque[Tuple[bytes, _Item, int]] = collections.deque()  # (frame, item, real samples)
        self._residual = bytearray()
        self._current: Optional[_Item] = None   # item new audio belongs to
        self._playing: Optional[_Item] = None   # item last played
        self._last_report = 0.0
        self._tasks: List[asyncio.Task] = []
        self._attached = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._closer: Optional[asyncio.Task] = None
        self._closed = False
        self.stats = {"in_frames": 0, "in_dropped": 0, "out_frames": 0, "out_stale": 0, "out_dropped": 0,
                      "flushes": 0, "played_reports": 0}

    def _spawn(self, coro: Any, label: str) -> asyncio.Task:
        started = False

        async def _guarded() -> None:
            nonlocal started
            started = True
            try:
                await coro
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception("call peer %s task failed: %s", label, exc)
                self._ended(f"{label}_failed")

        task = asyncio.ensure_future(_guarded())
        task.add_done_callback(lambda _t: coro.close() if not started else None)
        return task

    # -- lifecycle -----------------------------------------------------------------------------
    async def attach(self) -> None:
        if self._closed:
            raise PeerRefused("peer_attach_failed", self.closed_reason or "the call socket closed")
        try:
            # The audio frame bound applies to binary messages only: a caption (no byte cutoff at the host, a
            # final one carries its item's whole text) is read whole, so its item and played report stay exact.
            ws = await WebSocket.connect(self.host.ws_url(self.call_id), {"x-skill-token": self.host.token},
                                         timeout=ATTACH_TIMEOUT, max_size=cp.HEADER_SIZE + cp.MAX_FRAME_PAYLOAD)
        except WebSocketHandshakeError as exc:
            raise PeerRefused("peer_socket_refused", str(exc), exc.status) from None
        except (OSError, asyncio.TimeoutError) as exc:
            raise PeerRefused("peer_socket_failed", f"could not open the call socket: {exc}") from None
        self._ws = ws
        hello_sent = False
        try:
            if self._closed:
                raise PeerRefused("peer_attach_failed", self.closed_reason or "the call socket closed")
            await ws.send(cp.encode_control(cp.hello(self.rate, self.label)))
            hello_sent = True
            if self._closed:
                raise PeerRefused("peer_attach_failed", self.closed_reason or "the call socket closed")
            self._tasks = [self._spawn(self._reader(), "reader"), self._spawn(self._sender(), "sender")]
            try:
                await asyncio.wait_for(self._attached.wait(), ATTACH_TIMEOUT)
            except asyncio.TimeoutError:
                await self.close("attach_timeout")
                raise PeerRefused("peer_attach_failed", "the call did not answer the hello") from None
            if self._closed:
                raise PeerRefused("peer_attach_failed", self.closed_reason or "the call socket closed")
        except BaseException as exc:
            self._closed = True
            self._attached.set()
            async with self._close_lock:
                await self._cleanup_transport(self.closed_reason or "attach_failed", send_bye=hello_sent)
            if isinstance(exc, (ConnectionError, OSError, WebSocketClosed)) and not isinstance(exc, PeerRefused):
                raise PeerRefused("peer_socket_failed", f"could not open the call socket: {exc}") from None
            raise

    async def _cleanup_transport(self, reason: str = "hangup", *, send_bye: bool = True) -> None:
        current = asyncio.current_task()
        tasks, self._tasks = list(self._tasks), []
        other = [task for task in tasks if task is not current]
        for task in other:
            task.cancel()
        if other:
            await asyncio.gather(*other, return_exceptions=True)
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                if send_bye and not ws.closed:
                    async def _flush_and_bye() -> None:
                        while self._control_queue:
                            await ws.send(self._control_queue.popleft())
                        await ws.send(cp.encode_control({"type": "bye", "reason": reason[:64]}))

                    try:
                        await asyncio.wait_for(_flush_and_bye(), 2)
                    except (asyncio.TimeoutError, ConnectionError, OSError, WebSocketClosed, RuntimeError):
                        pass
                else:
                    self._control_queue.clear()
            finally:
                await ws.close()

    async def close(self, reason: str = "hangup") -> None:
        first_close = not self._closed
        if first_close:
            self._report_played(force=True)
            if not self.closed_reason:
                self.closed_reason = reason
            self._closed = True
        self._attached.set()
        async with self._close_lock:
            await self._cleanup_transport(reason, send_bye=first_close)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def connected(self) -> bool:
        """Something of the call socket is still open: the socket itself or its reader and sender. ``closed``
        turns true as soon as a close starts; this stays true until the close got through."""
        return self._ws is not None or any(not task.done() for task in self._tasks)

    # -- toward the hub ---------------------------------------------------------------------------
    def push_line_frame(self, pcm8k: bytes) -> None:
        """One 20 ms line frame (PCM16LE 8 kHz) toward the call."""
        if self._closed or self.epoch is None:
            return
        pcm = self._up.process(pcm8k)
        frame = cp.encode_frame(cp.AudioFrame(cp.FRAME_AUDIO_IN, self.epoch, self._seq, self.rate, 0, pcm))
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        if len(self._send_queue) >= SEND_QUEUE_FRAMES:
            self._send_queue.popleft()
            self.stats["in_dropped"] += 1
        self._send_queue.append(frame)
        self.stats["in_frames"] += 1
        self._send_wake.set()

    def send_context(self, text: str, source: str = "phone_line") -> None:
        self._send_control({"type": "context", "text": cp.bounded_text(text), "source": source})

    def _send_control(self, message: Dict[str, Any]) -> None:
        if not self._closed:
            self._control_queue.append(cp.encode_control(message))
            self._send_wake.set()

    async def _sender(self) -> None:
        try:
            while True:
                await self._send_wake.wait()
                self._send_wake.clear()
                while (self._control_queue or self._send_queue) and self._ws is not None:
                    if self._control_queue:  # line facts and playback reports go before queued audio
                        await self._ws.send(self._control_queue.popleft())
                    else:
                        await self._ws.send(self._send_queue.popleft())
        except asyncio.CancelledError:
            return
        except (ConnectionError, OSError, WebSocketClosed) as exc:
            self._ended(f"send_failed: {exc}")

    # -- from the hub ------------------------------------------------------------------------------
    async def _reader(self) -> None:
        reason = "socket_closed"
        try:
            while True:
                ws = self._ws
                if ws is None:
                    break
                message = await ws.recv()
                if isinstance(message, bytes):
                    self._audio_out(message)
                else:
                    self._control(message)
                    if self.state == cp.STATE_CLOSED:
                        reason = self.closed_reason or "call_closed"
                        await ws.close()
                        break
        except asyncio.CancelledError:
            return
        except WebSocketClosed as exc:
            reason = self.closed_reason or exc.reason or f"socket_closed_{exc.code}"
        except (ConnectionError, OSError) as exc:
            reason = self.closed_reason or f"socket_closed: {exc}"
        self._ended(reason)

    def _ended(self, reason: str) -> None:
        if not self.closed_reason:
            self.closed_reason = reason
        self._attached.set()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        if self._ws is not None and self._closer is None:
            # The socket goes with the peer, whoever ended it: a failed sender or a closed call leaves no
            # open connection behind, whether or not anyone calls close() afterwards.
            self._closer = asyncio.ensure_future(self._close_after_end(reason))
        if not self._closed:
            self._closed = True
            self.on_closed(reason)

    async def _close_after_end(self, reason: str) -> None:
        try:
            async with self._close_lock:
                await self._cleanup_transport(reason, send_bye=False)
        except Exception as exc:  # never an unretrieved task error
            log.warning("call peer socket close failed: %s", exc)

    def _control(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "flush" and isinstance(message.get("epoch"), int):
            first = self.epoch is None
            self.epoch = message["epoch"] & 0xFFFF
            if not first:
                self._flush()
            self._attached.set()
        elif kind == "state":
            self.state = str(message.get("state") or "")
            if self.state == cp.STATE_CLOSED:
                self.closed_reason = str(message.get("reason") or "call_closed")
                detail = str(message.get("detail") or "")
                if detail:
                    self.closed_reason += f": {detail}"
            self.on_state(self.state)
        elif kind == "caption":
            speaker = message.get("speaker")
            item_id = str(message.get("item_id") or "")
            if speaker == "ouroboros" and item_id and (self._current is None or self._current.item_id != item_id):
                self._finish_residual()
                self._current = _Item(item_id)
            self.on_caption(message)

    def _audio_out(self, data: bytes) -> None:
        try:
            frame = cp.decode_frame(data)
        except ValueError:
            return
        if frame.kind != cp.FRAME_AUDIO_OUT:
            return
        if self.epoch is not None and frame.epoch != self.epoch:
            self.stats["out_stale"] += 1
            return
        if frame.rate != self.rate:
            return  # the hub converts to the hello rate; anything else is not ours
        if self._current is None:
            self._current = _Item("")
        self._residual += self._down.process(frame.pcm)
        while len(self._residual) >= FRAME_BYTES:
            self._enqueue(bytes(self._residual[:FRAME_BYTES]), FRAME_BYTES // 2)
            del self._residual[:FRAME_BYTES]

    def _finish_residual(self) -> None:
        if self._residual and self._current is not None:
            real = len(self._residual) // 2
            self._enqueue(bytes(self._residual) + bytes(FRAME_BYTES - len(self._residual)), real)
        self._residual.clear()

    def _enqueue(self, frame: bytes, real_samples: int) -> None:
        if len(self._output) >= OUTPUT_LIMIT_FRAMES:
            self._output.popleft()
            self.stats["out_dropped"] += 1
        self._output.append((frame, self._current, real_samples))

    def _flush(self) -> None:
        self.stats["flushes"] += 1
        self._report_played(force=True)
        self._output.clear()
        self._residual.clear()
        self._down.reset()
        self._current = None

    # -- the media clock ------------------------------------------------------------------------------
    def pop_line_frame(self) -> Optional[bytes]:
        """This tick's 20 ms toward the line, or None (silence)."""
        if self.hold_output:
            return None
        if not self._output and self._residual and self._current is not None:
            # The answer ended mid-frame: play its tail padded with silence.
            self._finish_residual()
        if not self._output:
            self._report_played()
            return None
        frame, item, real = self._output.popleft()
        if self._playing is not item:
            self._report_played(force=True)
            self._playing = item
        item.played_samples += real
        self.stats["out_frames"] += 1
        # The last queued frame closes the answer for now: report it at once, not at the next 100 ms.
        self._report_played(force=not self._output)
        return frame

    def discard_output(self) -> None:
        """Drop queued speech without playing it (it was never heard by the line)."""
        self._output.clear()
        self._residual.clear()

    def _report_played(self, force: bool = False) -> None:
        item = self._playing
        if item is None or not item.item_id:
            return
        now = time.monotonic()
        played_ms = item.played_samples // 8
        if played_ms == item.reported_ms:
            return
        if not force and now - self._last_report < PLAYED_REPORT_MS / 1000:
            return
        self._last_report = now
        item.reported_ms = played_ms
        self.stats["played_reports"] += 1
        self._send_control({"type": "played", "item_id": item.item_id, "played_ms": played_ms})

    @property
    def queued_ms(self) -> int:
        return len(self._output) * 20
