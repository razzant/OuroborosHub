"""telephony: phone calls for Ouroboros through a pluggable provider line.

The ``phone_line`` companion holds the line (a SIP account by default) and joins
live calls as a meeting peer; these tools only talk to it over its private
control channel in the skill's state directory. The conversation itself is the
host's Live Conversation on whichever voice engine the owner selected.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any, Dict

from .phone import config as line_config
from .phone.control import ControlUnavailable, request

COMPANION = "phone_line"
DIAL_TIMEOUT_SEC = 75
CONTROL_TIMEOUT_SEC = 20

_STATE_DIR: pathlib.Path = pathlib.Path(".")
_API: Any = None


def _reply(payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    if payload.get("ok") is False:
        return f"⚠️ {payload.get('code') or 'PHONE_REFUSED'}: {payload.get('message') or ''}\n{body}"
    return body


def _ask(op: str, args: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    try:
        return request(_STATE_DIR, op, args, timeout_sec=timeout)
    except ControlUnavailable as exc:
        return {"ok": False, "code": "PHONE_COMPANION_UNAVAILABLE", "reason": exc.reason, "message": str(exc)}


def live_call_id(ctx: Any) -> str:
    """The live call this turn belongs to, if any.

    Current hosts record it as ``execution_transport`` (kind ``live_conversation``) in the task
    metadata; ``live_call`` is read too for hosts that published it under that name.
    """
    metadata = getattr(ctx, "task_metadata", None)
    if not isinstance(metadata, dict):
        return ""
    transport = metadata.get("execution_transport")
    if isinstance(transport, dict) and transport.get("kind") == "live_conversation":
        call_id = str(transport.get("call_id") or "").strip()
        if call_id:
            return call_id
    live = metadata.get("live_call")
    return str(live.get("call_id") or "").strip() if isinstance(live, dict) else ""


def phone_call(ctx: Any = None, *, number: str = "", purpose: str = "", brief: str = "",
               owner_approved: bool = False) -> str:
    args = {"number": number, "purpose": purpose, "brief": brief, "owner_approved": owner_approved is True,
            "live_call_id": live_call_id(ctx), "task_id": str(getattr(ctx, "task_id", "") or "")}
    return _reply(_ask("dial", args, DIAL_TIMEOUT_SEC))


def phone_hangup(ctx: Any = None) -> str:
    return _reply(_ask("hangup", {"reason": "agent_hangup"}, CONTROL_TIMEOUT_SEC))


def phone_send_dtmf(ctx: Any = None, *, digits: str = "") -> str:
    return _reply(_ask("dtmf", {"digits": digits}, CONTROL_TIMEOUT_SEC))


def phone_status(ctx: Any = None) -> str:
    return _reply({**_ask("status", {}, CONTROL_TIMEOUT_SEC), "in_live_call": bool(live_call_id(ctx))})


# -- routes -----------------------------------------------------------------------------------------


def _json(payload: Dict[str, Any], status: int = 200):
    from starlette.responses import JSONResponse
    return JSONResponse(payload, status_code=status)


def _account(config: Dict[str, Any]) -> str:
    sip = config.get("sip") or {}
    user, domain = str(sip.get("username") or ""), str(sip.get("domain") or sip.get("registrar") or "")
    return f"{user}@{domain}" if user and domain else ""


async def _status_route(_request: Any):
    await asyncio.to_thread(apply_saved_password, _API)
    config = await asyncio.to_thread(line_config.load, _STATE_DIR)
    live = await asyncio.to_thread(_ask, "status", {}, 5)
    line = (live.get("line") or {}) if live.get("ok") else {}
    state = line.get("registration") or ("companion not running" if not live.get("ok") else "unknown")
    active = (live.get("active_call") or live.get("line_busy")) if live.get("ok") else None
    account = _account(config)
    text = ("On a call" if active else
            "Registered" if state == "registered" else
            "Not configured" if not account else f"Line {state}")
    tone = "ok" if state == "registered" else "warn"
    payload: Dict[str, Any] = {
        "title": "Phone line", "text": text, "tone": tone, "line": line, "on_call": bool(active),
        "labels": {"direct_message": "A phone call to or from this line"},
    }
    payload["connect"] = [{"account_id": account, "conversation_id": "", "label": f"Line {account}"}] if account else []
    return _json(payload)


async def _settings_route(request: Any):
    if request.method == "GET":
        config = await asyncio.to_thread(line_config.load, _STATE_DIR)
        return _json(line_config.to_form(config))
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _json({"ok": False, "error": "Expected the phone line settings"}, 400)
    try:
        current = await asyncio.to_thread(line_config.load, _STATE_DIR)
        updated = line_config.from_form(current, body)
        await asyncio.to_thread(line_config.save, _STATE_DIR, updated)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    await asyncio.to_thread(apply_saved_password, _API)
    return _json({"ok": True, "message": "Saved. The line restarts with these settings between calls.",
                  **line_config.to_form(updated)})


REFRESH_SECONDS = 45
TERMINAL_REFRESH_RESULTS = ("access_closed", "not_server", "unsupported")


def apply_saved_password(api: Any, ask=None) -> str:
    """Give the companion the SIP password saved in Settings, between calls only.

    ``refresh_companion_process`` without ``expected`` compares the companion's settings environment with
    Settings itself (PluginAPI 2.4) and restarts it only when they differ, so this skill never reads the
    password. A restart is never requested during a call, while one is still being closed (``line_busy``), or
    when the companion's call state is unknown (for example, when a ``status`` check timed out). Because the
    plugin never sees the password, a narrow race remains if the password was just changed and a call starts in
    the milliseconds between an idle ``status`` reply and the supervisor's ``SIGTERM``; in that case ``Daemon``
    catches ``SIGTERM`` and hangs up cleanly within the supervisor's stop grace. Older hosts apply a new
    password when the skill restarts.
    """
    if api is None or not hasattr(api, "refresh_companion_process"):
        return "unsupported"
    try:
        status = (ask or _ask)("status", {}, 5)
    except ControlUnavailable as exc:
        if exc.reason != "not_running":
            return "deferred_status_unknown"
        status = {"ok": False, "reason": "not_running"}
    except Exception:
        return "deferred_status_unknown"
    if not isinstance(status, dict):
        return "deferred_status_unknown"
    if status.get("ok") is True:
        if status.get("active_call") or status.get("line_busy"):  # a call, or one still being closed
            return "deferred_call_active"
    elif status.get("reason") != "not_running":
        return "deferred_status_unknown"
    try:
        return str(api.refresh_companion_process(COMPANION))
    except Exception as exc:  # report, never raise into the host loop
        return f"error: {type(exc).__name__}"


def _password_watch(api: Any):
    async def watch() -> None:
        while True:
            outcome = await asyncio.to_thread(apply_saved_password, api)  # it blocks: off the host extension loop
            if outcome in TERMINAL_REFRESH_RESULTS:
                return
            await asyncio.sleep(REFRESH_SECONDS)
    return watch


SETTINGS_FIELDS = [
    {"name": "profile", "label": "Provider profile", "type": "select",
     "options": [{"value": "generic", "label": "Generic SIP (registrar or cloud PBX over UDP, IPv4, G.711)"},
                 {"value": "twilio", "label": "Twilio SIP Domain (registration)"}]},
    {"name": "registrar", "label": "Registrar host", "type": "text",
     "placeholder": "sip.example.com or mydomain.sip.twilio.com"},
    {"name": "port", "label": "Registrar port (UDP)", "type": "number", "placeholder": "5060"},
    {"name": "domain", "label": "SIP domain (if not the registrar)", "type": "text"},
    {"name": "username", "label": "SIP username", "type": "text"},
    {"name": "auth_username", "label": "Auth username (if different)", "type": "text"},
    {"name": "display_name", "label": "Display name", "type": "text"},
    {"name": "caller_id", "label": "Caller ID user part for outgoing calls (optional)", "type": "text"},
    {"name": "outbound_proxy", "label": "Outbound proxy host[:port] (optional)", "type": "text"},
    {"name": "expires", "label": "Registration interval, seconds", "type": "number", "placeholder": "300 (Twilio: 600-3600)"},
    {"name": "register", "label": "Register with the registrar (off for an IP-authenticated trunk)", "type": "checkbox"},
    {"name": "trusted_sources", "label": ("Extra hosts allowed to send calls: host names, IPv4 addresses or IPv4 "
                                          "ranges up to /8 such as 203.0.113.0/28, one per line (no IPv6)"),
     "type": "textarea"},
    {"name": "allowed_prefixes", "label": "Outgoing calls allowed to (one prefix per line, e.g. +7). Empty: no outgoing calls",
     "type": "textarea"},
    {"name": "extra_blocked_prefixes", "label": "Also never call (prefixes, one per line)", "type": "textarea"},
    {"name": "inbound_policy", "label": "Incoming calls", "type": "select",
     "options": [{"value": "off", "label": "Off: decline every call"},
                 {"value": "allowlist", "label": "Only from the numbers below"},
                 {"value": "all", "label": "From anyone (the Phone place still decides)"}]},
    {"name": "inbound_allowlist", "label": "Numbers allowed to call in (E.164, one per line)", "type": "textarea"},
    {"name": "max_call_seconds", "label": "Longest call, seconds (60-1800)", "type": "number", "placeholder": "600"},
    {"name": "calls_per_day", "label": "Calls per day (both directions)", "type": "number", "placeholder": "20"},
    {"name": "dial_cooldown_seconds", "label": "Pause between outgoing calls, seconds", "type": "number",
     "placeholder": "30"},
]


def register(api: Any) -> None:
    global _STATE_DIR, _API
    _STATE_DIR = pathlib.Path(api.get_state_dir())
    _API = api
    api.register_tool(
        "phone_call", phone_call,
        description=(
            "Place a phone call through the owner's phone line. Call only a number your owner named or confirmed "
            "for this purpose (owner_approved=true); otherwise ask them first. From the owner's live call, the "
            "person called joins that call and you talk to them there. From other work, the conversation runs as a "
            "Presence call in the Phone place, with this purpose and brief as its instructions. Returns while it "
            "rings; phone_status follows it. The line refuses numbers outside the allowed prefixes, premium or "
            "satellite ranges, a second simultaneous call, and calls beyond the daily limit."),
        schema={"type": "object", "properties": {
            "number": {"type": "string", "description": "E.164 number, e.g. +74951234567."},
            "purpose": {"type": "string", "description": "Why you call, in one or two sentences (said to no one; "
                                                        "it instructs the call)."},
            "brief": {"type": "string", "description": "What you may say and agree to, facts you may share, and "
                                                      "what needs the owner's confirmation first."},
            "owner_approved": {"type": "boolean", "description": "True only if your owner named this number or "
                                                                "confirmed calling it."},
        }, "required": ["number", "purpose", "owner_approved"], "additionalProperties": False},
        timeout_sec=DIAL_TIMEOUT_SEC + 15,
    )
    api.register_tool(
        "phone_hangup", phone_hangup,
        description="Hang up the phone call on the line (or stop it ringing). Say goodbye first.",
        schema={"type": "object", "properties": {}, "additionalProperties": False}, timeout_sec=CONTROL_TIMEOUT_SEC + 5,
    )
    api.register_tool(
        "phone_send_dtmf", phone_send_dtmf,
        description="Press keys on the answered phone call (voice menus, extensions): 0-9, *, #, A-D.",
        schema={"type": "object", "properties": {"digits": {"type": "string", "description": "Keys in order, e.g. 1 or 2#."}},
                "required": ["digits"], "additionalProperties": False}, timeout_sec=CONTROL_TIMEOUT_SEC + 5,
    )
    api.register_tool(
        "phone_status", phone_status,
        description=("The phone line and its call: registration, state (dialing, ringing, answered, ended), "
                     "duration, outcome and the transcript so far; the last call when none is active."),
        schema={"type": "object", "properties": {}, "additionalProperties": False}, timeout_sec=CONTROL_TIMEOUT_SEC + 5,
    )
    api.register_route("status", _status_route, methods=("GET",))
    api.register_route("settings/save", _settings_route, methods=("GET", "POST"))
    api.register_settings_section("connection", "Phone line", schema={"components": [
        {"type": "markdown", "text": (
            "The SIP password is a Settings secret (`TELEPHONY_SIP_PASSWORD`): ask Ouroboros to request it, "
            "and grant it to this skill; the line picks it up by itself between calls. Other settings are saved "
            "here; the line restarts with them between calls. "
            "Calls need Calls (beta) and a voice engine (Gemini Live, Grok or OpenAI Realtime) with its key.")},
        {"type": "form", "route": "settings/save", "method": "POST", "submit_label": "Save phone line settings",
         "fields": SETTINGS_FIELDS},
    ]})
    api.register_companion_process(COMPANION)
    if hasattr(api, "refresh_companion_process"):
        api.register_supervised_task("password_watch", _password_watch(api), restart_policy="on_failure",
                                     max_restarts=10)
