"""Hard limits of the phone line, enforced in the companion and never by the model.

* numbers are E.164; outgoing calls need an allowed country prefix (an empty
  list forbids every outgoing call) and never reach the built-in blocked
  prefixes (premium-rate, satellite and international shared-cost ranges),
  whatever the allow list says;
* one call at a time; a pause between dials; a daily cap on calls (both
  directions); a maximum duration (default 600 s, at most 1800 s);
* incoming calls: ``off`` (default), ``allowlist`` (listed numbers only) or ``all``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")

# Ranges where one call can cost real money or reach no person. The list is a floor;
# the owner can only add to it (``extra_blocked_prefixes``).
BLOCKED_PREFIXES: Tuple[str, ...] = (
    "+870", "+871", "+872", "+873", "+874",   # Inmarsat
    "+881", "+882", "+883",                   # global satellite and international networks
    "+888",                                   # OCHA / disaster relief
    "+979",                                   # international premium rate
    "+991",                                   # ITPCS trial
    "+808",                                   # international shared cost
    "+1900", "+1976",                         # North American premium rate
    "+7809", "+7803",                         # Russian premium-rate ranges (8-809, 8-803)
    "+449",                                   # UK 09 premium rate
    "+3389",                                  # France 089x premium rate
    "+49900", "+49137",                       # Germany premium / televoting
    "+3989",                                  # Italy 89x premium rate
)

DEFAULT_LIMITS = {"max_call_seconds": 600, "calls_per_day": 20, "dial_cooldown_seconds": 30,
                  "ring_timeout_seconds": 45}
MAX_CALL_SECONDS_CAP = 1800
INBOUND_POLICIES = ("off", "allowlist", "all")


class PolicyRefusal(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def normalize_number(raw: str) -> str:
    """E.164 or a refusal; spaces, dashes, dots and parentheses are dropped, ``00`` becomes ``+``."""
    text = re.sub(r"[\s\-().]", "", str(raw or ""))
    if text.startswith("00"):
        text = "+" + text[2:]
    if not E164.match(text):
        raise PolicyRefusal("PHONE_NUMBER_INVALID",
                            "The number must be in international E.164 form, such as +74951234567.")
    return text


CALLER_LITERAL = re.compile(r"^[+0-9*#]{1,32}$")


def caller_identity(raw: str) -> str:
    """How an incoming caller is named and compared: E.164 when the network gave an international number
    (``+...`` or ``00...``), otherwise the digits exactly as given (a national ``8916...`` stays ``8916...``,
    the country is never guessed), and ``unknown`` for anything that is not digits, ``+``, ``*`` or ``#``."""
    text = re.sub(r"[-.()]", "", str(raw or ""))
    if not CALLER_LITERAL.match(text):
        return "unknown"
    if text.startswith("+") or text.startswith("00"):
        try:
            return normalize_number(text)
        except PolicyRefusal:
            return text
    return text


def _prefixes(values: Iterable[Any]) -> List[str]:
    out = []
    for value in values or []:
        text = re.sub(r"[\s\-().]", "", str(value))
        if not text:
            continue
        if not text.startswith("+"):
            text = "+" + text
        if re.fullmatch(r"\+[0-9]{1,14}", text):
            out.append(text)
    return out


@dataclass
class Limits:
    max_call_seconds: int = 600
    calls_per_day: int = 20
    dial_cooldown_seconds: int = 30
    ring_timeout_seconds: int = 45

    @classmethod
    def from_config(cls, raw: Dict[str, Any]) -> "Limits":
        merged = {**DEFAULT_LIMITS, **{k: v for k, v in (raw or {}).items() if k in DEFAULT_LIMITS}}

        def bounded(key, low, high):
            raw_val = merged[key]
            if isinstance(raw_val, bool):
                value = DEFAULT_LIMITS[key]
            else:
                try:
                    text = str(raw_val).strip()
                    value = int(raw_val) if isinstance(raw_val, int) else (
                        int(text) if text and (text.lstrip("-").isascii() and text.lstrip("-").isdigit()) else None
                    )
                    if value is None:
                        value = DEFAULT_LIMITS[key]
                except (TypeError, ValueError):
                    value = DEFAULT_LIMITS[key]
            return max(low, min(high, value))

        return cls(max_call_seconds=bounded("max_call_seconds", 60, MAX_CALL_SECONDS_CAP),
                   calls_per_day=bounded("calls_per_day", 1, 200),
                   dial_cooldown_seconds=bounded("dial_cooldown_seconds", 5, 3600),
                   ring_timeout_seconds=bounded("ring_timeout_seconds", 10, 120))


class Policy:
    def __init__(self, config: Dict[str, Any], state_dir: Path):
        outbound = config.get("outbound") or {}
        inbound = config.get("inbound") or {}
        self.allowed_prefixes = _prefixes(outbound.get("allowed_prefixes"))
        self.blocked_prefixes = tuple(BLOCKED_PREFIXES) + tuple(_prefixes(outbound.get("extra_blocked_prefixes")))
        self.inbound_policy = str(inbound.get("policy") or "off")
        if self.inbound_policy not in INBOUND_POLICIES:
            self.inbound_policy = "off"
        allow = []
        for value in inbound.get("allowlist") or []:
            text = re.sub(r"[\s\-().]", "", str(value))
            identity = caller_identity(text)
            if identity != "unknown":
                allow.append(identity)  # E.164 entries normalised, national ones literal
        self.inbound_allowlist = tuple(dict.fromkeys(allow))
        self.limits = Limits.from_config(config.get("limits") or {})
        self.usage = UsageCounter(Path(state_dir) / "usage.json")
        self.last_dial_at = 0.0

    def check_outbound(self, number: str, now: float) -> str:
        normalized = normalize_number(number)
        if any(normalized.startswith(p) for p in self.blocked_prefixes):
            raise PolicyRefusal("PHONE_NUMBER_BLOCKED",
                                "This number is in a premium-rate or satellite range that the phone line never calls.")
        if not self.allowed_prefixes:
            raise PolicyRefusal("PHONE_OUTBOUND_DISABLED",
                                "Outgoing calls are off: the owner has not allowed any country prefix in the "
                                "telephony settings.")
        if not any(normalized.startswith(p) for p in self.allowed_prefixes):
            raise PolicyRefusal("PHONE_NUMBER_NOT_ALLOWED",
                                f"Outgoing calls are allowed only to {', '.join(self.allowed_prefixes)}.")
        wait = self.limits.dial_cooldown_seconds - (now - self.last_dial_at)
        if self.last_dial_at and wait > 0:
            raise PolicyRefusal("PHONE_COOLDOWN", f"The line allows the next call in {int(wait) + 1} s.")
        if self.usage.today() >= self.limits.calls_per_day:
            raise PolicyRefusal("PHONE_DAILY_LIMIT",
                                f"Today's limit of {self.limits.calls_per_day} calls is used up.")
        return normalized

    def inbound_decision(self, number: str) -> Tuple[bool, int, str]:
        """(accept, SIP status for a refusal, reason)."""
        if self.inbound_policy == "off":
            return False, 480, "inbound_off"
        if self.usage.today() >= self.limits.calls_per_day:
            return False, 486, "daily_limit"
        if self.inbound_policy == "allowlist":
            identity = caller_identity(number)
            if identity == "unknown" or identity not in self.inbound_allowlist:
                return False, 603, "caller_not_allowed"
        return True, 0, ""


class UsageCounter:
    """Calls per local calendar day, kept in the skill's state directory."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _day(today: Optional[_dt.date] = None) -> str:
        return (today or _dt.date.today()).isoformat()

    def today(self) -> int:
        with self._lock:
            return self._read().get(self._day(), 0)

    def add(self) -> int:
        with self._lock:
            data = self._read()
            day = self._day()
            data = {k: v for k, v in data.items() if k >= self._day(_dt.date.today() - _dt.timedelta(days=7))}
            data[day] = data.get(day, 0) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(data))
            os.replace(tmp, self.path)
            return data[day]
