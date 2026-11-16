"""The line's settings document: ``config.json`` in the skill's state directory.

Everything except the SIP password lives here; the password is the Settings
secret ``TELEPHONY_SIP_PASSWORD`` (``secret_fields``), which reaches only the
companion's environment. The form in the skill's settings section writes this
file; the companion re-reads it when it changes.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

CONFIG_FILE = "config.json"
PASSWORD_KEY = "TELEPHONY_SIP_PASSWORD"
PROFILES = ("generic", "twilio")
MIN_TRUSTED_PREFIX = 8   # a trusted CIDR range is at most a /8 (16.7 million addresses)

DEFAULTS: Dict[str, Any] = {
    "provider": "sip",
    "profile": "generic",
    "sip": {"registrar": "", "port": 5060, "domain": "", "username": "", "auth_username": "",
            "display_name": "", "outbound_proxy": "", "register": True, "expires": 300, "caller_id": "",
            "dial_domain": "", "local_host": "", "local_port": 0, "trusted_sources": [], "sdp_address": "auto"},
    "outbound": {"allowed_prefixes": [], "extra_blocked_prefixes": []},
    "inbound": {"policy": "off", "allowlist": []},
    "limits": {"max_call_seconds": 600, "calls_per_day": 20, "dial_cooldown_seconds": 30, "ring_timeout_seconds": 45},
}

# Flat form fields <-> nested document.
FORM_FIELDS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("profile", ("profile",), "str"),
    ("registrar", ("sip", "registrar"), "str"),
    ("port", ("sip", "port"), "int"),
    ("domain", ("sip", "domain"), "str"),
    ("username", ("sip", "username"), "str"),
    ("auth_username", ("sip", "auth_username"), "str"),
    ("display_name", ("sip", "display_name"), "str"),
    ("caller_id", ("sip", "caller_id"), "str"),
    ("outbound_proxy", ("sip", "outbound_proxy"), "str"),
    ("expires", ("sip", "expires"), "int"),
    ("register", ("sip", "register"), "bool"),
    ("trusted_sources", ("sip", "trusted_sources"), "list"),
    ("allowed_prefixes", ("outbound", "allowed_prefixes"), "list"),
    ("extra_blocked_prefixes", ("outbound", "extra_blocked_prefixes"), "list"),
    ("inbound_policy", ("inbound", "policy"), "str"),
    ("inbound_allowlist", ("inbound", "allowlist"), "list"),
    ("max_call_seconds", ("limits", "max_call_seconds"), "int"),
    ("calls_per_day", ("limits", "calls_per_day"), "int"),
    ("dial_cooldown_seconds", ("limits", "dial_cooldown_seconds"), "int"),
)


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(state_dir: Path) -> Dict[str, Any]:
    path = Path(state_dir) / CONFIG_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return _merge(DEFAULTS, data if isinstance(data, dict) else {})


def save(state_dir: Path, config: Dict[str, Any]) -> None:
    path = Path(state_dir) / CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def to_form(config: Dict[str, Any]) -> Dict[str, Any]:
    form: Dict[str, Any] = {}
    for name, path, kind in FORM_FIELDS:
        value: Any = config
        for key in path:
            value = value.get(key, "") if isinstance(value, dict) else ""
        if kind == "list":
            form[name] = "\n".join(str(v) for v in value)
        elif kind == "bool":
            form[name] = bool(value)
        else:
            form[name] = str(value)
    return form


def _split_lines(text: Any) -> List[str]:
    if isinstance(text, list):
        items = text
    else:
        items = re.split(r"[\n,;]+", str(text or ""))
    return [str(item).strip() for item in items if str(item).strip()]


def from_form(current: Dict[str, Any], form: Dict[str, Any]) -> Dict[str, Any]:
    """Apply submitted form fields to ``current``; raises ValueError naming a bad field."""
    config = copy.deepcopy(current)
    known = {name for name, _, _ in FORM_FIELDS}
    unknown = set(form) - known
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    for name, path, kind in FORM_FIELDS:
        if name not in form:
            continue
        raw = form[name]
        if kind == "int":
            text = str(raw).strip()
            if not text:
                continue
            if not (text.isascii() and text.isdigit()):
                raise ValueError(f"{name} must be a whole number")
            value: Any = int(text)
        elif kind == "list":
            value = _split_lines(raw)
        elif kind == "bool":
            value = raw is True or str(raw).strip().lower() in ("true", "1", "on", "yes")
        else:
            value = str(raw or "").strip()
            if len(value) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError(f"{name} must be one short line without control characters")
        target = config
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value
    validate(config)
    if config.get("profile") not in PROFILES:
        raise ValueError("profile must be generic or twilio")
    if config["inbound"].get("policy") not in ("off", "allowlist", "all"):
        raise ValueError("inbound_policy must be off, allowlist or all")
    if config["profile"] == "twilio":
        registrar = str(config["sip"].get("registrar") or "")
        if registrar and not registrar.endswith(".twilio.com"):
            raise ValueError("a Twilio SIP Domain registrar ends with .twilio.com, e.g. mydomain.sip.twilio.com")
        expires = int(config["sip"].get("expires") or 600)
        config["sip"]["expires"] = min(max(expires, 600), 3600)
    return config


HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*\.?$")
SIP_USER = re.compile(r"^[A-Za-z0-9_.!~*'()&=+$,%-]{1,64}$")   # RFC 3261 user chars without ; ? / @ : < >
DISPLAY = re.compile(r'^[^"\\<>\x00-\x1f\x7f]{0,64}$')


def _host_ok(text: str) -> bool:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        if ":" in text or all(part.isdigit() for part in text.rstrip(".").split(".")):
            return False
        return bool(HOST.match(text))


def _hostport_ok(text: str) -> bool:
    text = text.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        host = f"[{host}]"
        port = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            return False
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    else:
        host, port = text, ""
    if port and not (port.isascii() and port.isdigit() and 0 < int(port) < 65536):
        return False
    return _host_ok(host)


def validate(config: Dict[str, Any]) -> None:
    """Everything that reaches SIP headers or sockets: a bad value is refused here, never at the line's start."""
    sip = config.get("sip") or {}
    for key in ("registrar", "domain", "dial_domain"):
        value = str(sip.get(key) or "")
        if value and not _host_ok(value):
            raise ValueError(f"{key} must be a host name or an IP address")
    proxy = str(sip.get("outbound_proxy") or "")
    if proxy and not _hostport_ok(proxy):
        raise ValueError("outbound_proxy must be host or host:port with a port 1-65535")
    for key, low in (("port", 1), ("local_port", 0)):
        value = sip.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value < 65536:
            raise ValueError(f"{key} must be {low}-65535")
    for key in ("username", "auth_username", "caller_id"):
        value = str(sip.get(key) or "")
        if value and not SIP_USER.match(value):
            raise ValueError(f"{key} may hold letters, digits and - _ . ! ~ * ' ( ) & = + $ , % only")
    if not DISPLAY.match(str(sip.get("display_name") or "")):
        raise ValueError("display_name must be at most 64 characters without quotes, backslashes or < >")
    for source in sip.get("trusted_sources") or []:
        text = str(source).strip()
        if not text:
            continue
        stripped = text[1:-1] if text.startswith("[") and text.endswith("]") else text
        try:
            network = ipaddress.ip_network(stripped, strict=False)
        except ValueError:
            if ":" in text or not _host_ok(text):
                raise ValueError(f"trusted source {text!r} is not an IPv4 address, IPv4 CIDR range or host name")
            continue
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError(f"IPv6 trusted source {text!r} is not supported (the SIP line uses IPv4 UDP)")
        if network.prefixlen < MIN_TRUSTED_PREFIX:
            raise ValueError(f"trusted source {text!r} is wider than /{MIN_TRUSTED_PREFIX} and would trust a large "
                             "part of the internet; list the provider's own ranges")


def fingerprint(config: Dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True)
