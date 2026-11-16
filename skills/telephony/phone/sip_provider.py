"""The SIP adapter: one registered SIP account as a telephony provider.

Profiles only fill defaults; every field stays editable:

* ``generic`` — a SIP registrar or trunk over UDP, IPv4 and G.711 (cloud PBX,
  office PBX, trunk that accepts registration): host, port, user, auth user, domain.
* ``twilio`` — a Twilio SIP Domain with SIP Registration enabled:
  registrar ``<domain>.sip.twilio.com`` (or an edge host such as
  ``<domain>.sip.<edge>.twilio.com``), registration interval 600..3600 s,
  numbers dialled as E.164 in the Request-URI user part. Twilio's TwiML Bins
  route calls to and from the registration (see the skill README).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .provider import Leg, LegEvent, ProviderError, TelephonyProvider, register_provider
from .sip_ua import SipCall, SipConfig, SipError, SipUA, split_hostport


class SipLeg(Leg):
    def __init__(self, call: SipCall):
        self.call = call

    id = property(lambda self: self.call.id)
    direction = property(lambda self: self.call.direction)
    remote_number = property(lambda self: self.call.remote_number)
    state = property(lambda self: self.call.state)
    end_reason = property(lambda self: self.call.end_reason)
    end_status = property(lambda self: self.call.end_status)

    def read_frame(self) -> bytes:
        return self.call.read_frame()

    def write_frame(self, pcm: bytes) -> None:
        self.call.write_frame(pcm)

    async def send_dtmf(self, digits: str) -> str:
        try:
            return await self.call.send_dtmf(digits)
        except SipError as exc:
            raise ProviderError(exc.code, str(exc)) from None

    async def hangup(self, reason: str = "local_hangup") -> None:
        await self.call.hangup(reason)

    def ringing(self) -> None:
        self.call.ringing()

    async def accept(self) -> None:
        try:
            await self.call.accept()
        except SipError as exc:
            raise ProviderError(exc.code, str(exc)) from None

    async def reject(self, status: int = 486, reason: str = "") -> None:
        await self.call.reject(status, reason)

    def last_media_at(self) -> float:
        return self.call.last_media_at()

    @property
    def on_hold(self) -> bool:
        return self.call.on_hold

    def facts(self) -> Dict[str, Any]:
        call = self.call
        facts: Dict[str, Any] = {"jitter": dict(call.jitter.stats), **call.stats}
        if call.negotiated is not None:
            facts["codec"] = call.negotiated.codec
            facts["dtmf"] = "rfc4733" if call.negotiated.dtmf_pt is not None else "sip_info"
        if call.rtp is not None:
            facts["rtp"] = dict(call.rtp.stats)
            if call.rtp.remote:
                facts["remote_media"] = f"{call.rtp.remote[0]}:{call.rtp.remote[1]}"
        return facts


def sip_config(config: Dict[str, Any], password: str) -> SipConfig:
    from .config import validate
    try:
        validate(config)  # config.json may have been edited by hand: refuse here, typed, instead of crashing
    except ValueError as exc:
        raise ProviderError("not_configured", f"bad SIP settings: {exc}") from None
    sip = dict(config.get("sip") or {})
    profile = str(config.get("profile") or "generic")
    registrar = str(sip.get("registrar") or "").strip()
    if not registrar:
        raise ProviderError("not_configured", "the SIP registrar host is not set")
    username = str(sip.get("username") or "").strip()
    if not username:
        raise ProviderError("not_configured", "the SIP username is not set")
    expires = int(sip.get("expires") or (600 if profile == "twilio" else 300))
    if profile == "twilio":
        expires = min(max(expires, 600), 3600)  # Twilio refuses less than 600 (423) and caps at 3600
    trusted = tuple(str(s).strip() for s in (sip.get("trusted_sources") or []) if str(s).strip())
    proxy = str(sip.get("outbound_proxy") or "").strip()
    try:
        if proxy:
            split_hostport(proxy)
        port = int(sip.get("port") or 5060)
        local_port = int(sip.get("local_port") or 0)
    except (SipError, TypeError, ValueError) as exc:
        raise ProviderError("not_configured", f"bad SIP settings: {exc}") from None
    if not 0 < port < 65536 or not 0 <= local_port < 65536:
        raise ProviderError("not_configured", "SIP ports must be 1-65535")
    return SipConfig(
        registrar=registrar, username=username, password=password,
        registrar_port=port, domain=str(sip.get("domain") or "").strip(),
        auth_username=str(sip.get("auth_username") or "").strip(),
        display_name=str(sip.get("display_name") or "").strip(),
        outbound_proxy=proxy,
        register=bool(sip.get("register", True)), expires=expires,
        local_host=str(sip.get("local_host") or "").strip(), local_port=local_port,
        trusted_sources=trusted, caller_id=str(sip.get("caller_id") or "").strip(),
        dial_domain=str(sip.get("dial_domain") or "").strip(),
        sdp_address=str(sip.get("sdp_address") or "auto").strip(),
    )


class SipProvider(TelephonyProvider):
    name = "sip"

    def __init__(self, config: Dict[str, Any], password: str, on_incoming: Callable[[Leg], None],
                 on_status: Callable[[Dict[str, Any]], None]):
        super().__init__(on_incoming, on_status)
        self.sip = sip_config(config, password)
        self.profile = str(config.get("profile") or "generic")
        self.ua = SipUA(self.sip, on_incoming=lambda call: self.on_incoming(SipLeg(call)),
                        on_registration=lambda state, data: self.on_status(self.status()))

    async def start(self) -> None:
        try:
            await self.ua.start()
        except (OSError, SipError, ValueError) as exc:
            try:
                await self.ua.stop()
            except Exception:
                pass
            if isinstance(exc, SipError) and exc.code == "stopped":  # the line was stopped while it started
                raise ProviderError("stopped", str(exc)) from None
            raise ProviderError("start_failed", f"the SIP line could not start: {exc}") from None

    async def stop(self, deadline: Optional[float] = None) -> None:
        await self.ua.stop(deadline)

    async def dial(self, number: str, on_event: LegEvent) -> Leg:
        holder: Dict[str, SipLeg] = {}

        def relay(call: SipCall, kind: str, data: dict) -> None:
            leg = holder.get("leg")
            if leg is None:
                leg = holder["leg"] = SipLeg(call)
            on_event(leg, kind, data)

        try:
            call = await self.ua.dial(number, on_event=relay)
        except SipError as exc:  # "stopped": the line is shutting down; the manager reports a cancelled call
            raise ProviderError(exc.code, str(exc)) from None
        return holder.setdefault("leg", SipLeg(call))

    def bind_events(self, leg: Leg, on_event: LegEvent) -> None:
        assert isinstance(leg, SipLeg)
        leg.call.on_event = lambda call, kind, data: on_event(leg, kind, data)

    def account_id(self) -> str:
        return f"{self.sip.username}@{self.sip.aor_domain}"

    def status(self) -> Dict[str, Any]:
        reg = dict(self.ua.registration)
        return {"provider": "sip", "profile": self.profile, "account": self.account_id(),
                "registration": reg.get("state"), "registration_detail": reg.get("detail", ""),
                "registration_expires": reg.get("expires", 0),
                "local": f"{self.ua.local_host}:{self.ua.local_port}",
                "public": f"{self.ua.public[0]}:{self.ua.public[1]}" if self.ua.public else "",
                "stats": dict(self.ua.stats)}


register_provider("sip", lambda config, secret, on_incoming, on_status:
                  SipProvider(config, secret, on_incoming, on_status))
