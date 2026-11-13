"""The provider seam: what any telephony adapter gives the call manager.

An adapter turns one provider's signalling and media into line legs that speak
8 kHz PCM16 in 20 ms frames. The call manager never sees SIP, RTP or a
provider API; it sees a ``Leg``:

* ``read_frame()`` returns this tick's 20 ms of received audio (silence if none);
* ``write_frame(pcm)`` takes this tick's 20 ms of audio to send;
* ``clear_output()`` drops audio the provider itself still buffers (a provider
  with its own playout queue, such as a media-stream websocket, needs it on
  interruption; RTP sends every frame as it is written and needs nothing);
* ``send_dtmf``, ``hangup``, and for incoming legs ``ringing``/``accept``/``reject``.

Events reach the manager through one callback ``on_event(leg, kind, data)`` with
``kind`` in ``ringing``, ``answered``, ``dtmf`` (``digit``), ``hold`` and
``ended`` (``reason``, ``status``). New adapters register a factory in
``PROVIDERS``; a media-stream adapter (a provider that pushes 8 kHz mu-law over
a websocket) implements the same ``Leg`` with its own buffering and needs no
change elsewhere.
"""

from __future__ import annotations

import abc
from typing import Any, Callable, Dict, Optional

LegEvent = Callable[["Leg", str, dict], None]


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class Leg(abc.ABC):
    id: str
    direction: str          # "in" | "out"
    remote_number: str
    state: str              # new | incoming | ringing | answered | ended
    end_reason: str = ""
    end_status: int = 0

    @abc.abstractmethod
    def read_frame(self) -> bytes: ...

    @abc.abstractmethod
    def write_frame(self, pcm: bytes) -> None: ...

    def clear_output(self) -> None:
        """Drop provider-side buffered output (no-op for RTP)."""

    @abc.abstractmethod
    async def send_dtmf(self, digits: str) -> str: ...

    @abc.abstractmethod
    async def hangup(self, reason: str = "local_hangup") -> None: ...

    def ringing(self) -> None:
        """Incoming legs: tell the caller it rings (180)."""

    async def accept(self) -> None:
        raise ProviderError("not_incoming", "only incoming legs are answered")

    async def reject(self, status: int = 486, reason: str = "") -> None:
        raise ProviderError("not_incoming", "only incoming legs are rejected")

    def facts(self) -> Dict[str, Any]:
        return {}

    def last_media_at(self) -> float:
        """Monotonic time of the last media received (0.0: none yet); the manager's silence timeout reads it."""
        return 0.0

    @property
    def on_hold(self) -> bool:
        return False


class TelephonyProvider(abc.ABC):
    """One configured line of one provider."""

    name = "provider"

    def __init__(self, on_incoming: Callable[[Leg], None], on_status: Callable[[Dict[str, Any]], None]):
        self.on_incoming = on_incoming
        self.on_status = on_status

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self, deadline: Optional[float] = None) -> None:
        """End every leg still up (its BYE or CANCEL) and the line's own session (an unregistration), wait for them
        together until ``deadline`` (``time.monotonic()``; the adapter's own bound if None), then close."""

    @abc.abstractmethod
    async def dial(self, number: str, on_event: LegEvent) -> Leg: ...

    @abc.abstractmethod
    def bind_events(self, leg: Leg, on_event: LegEvent) -> None:
        """Route an incoming leg's events to ``on_event`` (outgoing legs get it from ``dial``)."""

    @abc.abstractmethod
    def status(self) -> Dict[str, Any]: ...

    def account_id(self) -> str:
        """Stable id of this line for places (the address the provider knows it by)."""
        return self.name


ProviderFactory = Callable[[Dict[str, Any], str, Callable[[Leg], None], Callable[[Dict[str, Any]], None]],
                           TelephonyProvider]
PROVIDERS: Dict[str, ProviderFactory] = {}


def register_provider(kind: str, factory: ProviderFactory) -> None:
    PROVIDERS[kind] = factory


def create_provider(config: Dict[str, Any], secret: str, on_incoming: Callable[[Leg], None],
                    on_status: Callable[[Dict[str, Any]], None]) -> TelephonyProvider:
    kind = str(config.get("provider") or "sip")
    if kind not in PROVIDERS:
        from . import sip_provider  # noqa: F401  (registers the built-in adapter)
    factory: Optional[ProviderFactory] = PROVIDERS.get(kind)
    if factory is None:
        raise ProviderError("unknown_provider", f"no telephony adapter named {kind!r}")
    return factory(config, secret, on_incoming, on_status)
