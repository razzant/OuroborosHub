"""SIP messages (RFC 3261): parsing, building and the header pieces a user agent needs."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

COMPACT = {"i": "Call-ID", "m": "Contact", "e": "Content-Encoding", "l": "Content-Length",
           "c": "Content-Type", "f": "From", "s": "Subject", "k": "Supported", "t": "To", "v": "Via"}
CANONICAL = {name.lower(): name for name in (
    "Via", "From", "To", "Call-ID", "CSeq", "Contact", "Max-Forwards", "Content-Type", "Content-Length",
    "Route", "Record-Route", "Expires", "Min-Expires", "Allow", "Supported", "User-Agent", "Server",
    "WWW-Authenticate", "Proxy-Authenticate", "Authorization", "Proxy-Authorization", "Reason",
    "Retry-After", "Event", "Accept", "Require", "Unsupported", "Session-Expires", "Warning",
    "P-Asserted-Identity", "Remote-Party-ID")}
CANONICAL["call-id"] = "Call-ID"
CANONICAL["cseq"] = "CSeq"
CANONICAL["www-authenticate"] = "WWW-Authenticate"
# Headers whose comma-separated values are separate header fields (RFC 3261 §7.3.1).
MULTI = {"via", "contact", "route", "record-route"}
MAGIC_COOKIE = "z9hG4bK"
MAX_MESSAGE_BYTES = 65535

REASONS = {100: "Trying", 180: "Ringing", 181: "Call Is Being Forwarded", 182: "Queued",
           183: "Session Progress", 200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
           403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 407: "Proxy Authentication Required",
           408: "Request Timeout", 415: "Unsupported Media Type", 420: "Bad Extension",
           423: "Interval Too Brief", 480: "Temporarily Unavailable", 481: "Call/Transaction Does Not Exist",
           482: "Loop Detected", 483: "Too Many Hops", 486: "Busy Here", 487: "Request Terminated",
           488: "Not Acceptable Here", 491: "Request Pending", 500: "Server Internal Error",
           501: "Not Implemented", 503: "Service Unavailable", 600: "Busy Everywhere", 603: "Decline"}


class SipParseError(ValueError):
    pass


def ascii_int(text: str, default=None):
    """An ASCII decimal integer from the network, or ``default`` (``"²".isdigit()`` is True, ``int`` refuses it)."""
    text = str(text or "").strip()
    return int(text) if text and text.isascii() and text.isdigit() else default


def _parse_port(text: str, context: str) -> int:
    port = ascii_int(text)
    if port is None or not 1 <= port <= 65535:
        raise SipParseError(f"bad port in {context!r}")
    return port


def canonical(name: str) -> str:
    lower = name.strip().lower()
    lower = COMPACT.get(lower, lower).lower() if len(lower) == 1 else lower
    return CANONICAL.get(lower, "-".join(part.capitalize() for part in lower.split("-")))


def split_commas(value: str) -> List[str]:
    """Split a header value on commas outside quotes and angle brackets."""
    parts, current, quoted, angle, escaped = [], [], False, 0, False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quoted:
            current.append(char)
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted and char == "<":
            angle += 1
        elif not quoted and char == ">":
            angle = max(0, angle - 1)
        if char == "," and not quoted and not angle:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current or not parts:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def parse_params(text: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        key, _, val = item.partition("=")
        params[key.strip().lower()] = val.strip().strip('"')
    return params


@dataclass
class SipUri:
    scheme: str = "sip"
    user: str = ""
    host: str = ""
    port: Optional[int] = None
    params: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "SipUri":
        text = text.strip()
        scheme, sep, rest = text.partition(":")
        if not sep or scheme.lower() not in ("sip", "sips", "tel"):
            raise SipParseError(f"not a SIP URI: {text!r}")
        rest = rest.split("?", 1)[0]
        hostpart, _, param_text = rest.partition(";")
        user = ""
        if "@" in hostpart:
            user, _, hostpart = hostpart.rpartition("@")
            user = user.split(":", 1)[0]
        host, port = hostpart, None
        if hostpart.startswith("["):
            end = hostpart.find("]")
            if end <= 1:
                raise SipParseError(f"bad IPv6 host in {text!r}")
            host = hostpart[:end + 1]
            tail = hostpart[end + 1:]
            if tail.startswith(":"):
                port = _parse_port(tail[1:], text)
            elif tail:
                raise SipParseError(f"bad hostport in {text!r}")
        elif ":" in hostpart:
            host, _, port_text = hostpart.rpartition(":")
            if ":" in host:
                raise SipParseError(f"unbracketed IPv6 host in {text!r}")
            port = _parse_port(port_text, text)
        if not host:
            raise SipParseError(f"empty host in {text!r}")
        params = parse_params(param_text)
        if "port" in params:
            _parse_port(params["port"], text)
        return cls(scheme.lower(), user, host, port, params)

    def __str__(self) -> str:
        out = f"{self.scheme}:"
        if self.user:
            out += f"{self.user}@"
        out += self.host
        if self.port:
            out += f":{self.port}"
        for key, val in self.params.items():
            out += f";{key}={val}" if val != "" else f";{key}"
        return out

    def address(self, default_port: int = 5060) -> Tuple[str, int]:
        if self.scheme not in ("sip", "sips"):
            raise SipParseError(f"cannot address {self.scheme}: URI")
        host = self.host.strip("[]")
        if not host:
            raise SipParseError("empty host in SIP URI")
        if self.port is not None:
            if not 1 <= self.port <= 65535:
                raise SipParseError(f"bad port {self.port} in {self!s}")
            return host, self.port
        if self.params.get("port"):
            return host, _parse_port(self.params["port"], str(self))
        if not 1 <= default_port <= 65535:
            raise SipParseError(f"bad default port {default_port}")
        return host, default_port


@dataclass
class NameAddr:
    display: str
    uri: SipUri
    params: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "NameAddr":
        text = text.strip()
        if "<" in text:
            display, _, rest = text.partition("<")
            uri_text, close, param_text = rest.partition(">")
            if not close:
                raise SipParseError(f"unclosed angle bracket in {text!r}")
            display = display.strip().strip('"')
        else:
            uri_text, _, param_text = text.partition(";")
            param_text = ";" + param_text if param_text else ""
            display = ""
        return cls(display, SipUri.parse(uri_text), parse_params(param_text))

    @property
    def tag(self) -> str:
        return self.params.get("tag", "")

    def __str__(self) -> str:
        out = (f'"{self.display}" ' if self.display else "") + f"<{self.uri}>"
        for key, val in self.params.items():
            out += f";{key}={val}" if val != "" else f";{key}"
        return out


@dataclass
class Via:
    transport: str
    host: str
    port: Optional[int]
    params: Dict[str, str]

    @classmethod
    def parse(cls, text: str) -> "Via":
        protocol, _, rest = text.strip().partition(" ")
        parts = protocol.split("/")
        if len(parts) != 3:
            raise SipParseError(f"bad Via: {text!r}")
        sent_by, _, param_text = rest.strip().partition(";")
        sent_by = sent_by.strip()
        host, port = sent_by, None
        if sent_by.startswith("["):
            end = sent_by.find("]")
            if end <= 1:
                raise SipParseError(f"bad Via host: {text!r}")
            host = sent_by[:end + 1]
            tail = sent_by[end + 1:]
            if tail.startswith(":"):
                port = _parse_port(tail[1:], text)
            elif tail:
                raise SipParseError(f"bad Via: {text!r}")
        elif ":" in sent_by:
            host, _, port_text = sent_by.rpartition(":")
            if ":" in host:
                raise SipParseError(f"bad Via host: {text!r}")
            port = _parse_port(port_text, text)
        if not host.strip("[]"):
            raise SipParseError(f"bad Via host: {text!r}")
        return cls(parts[2].upper(), host, port, parse_params(param_text))

    @property
    def branch(self) -> str:
        return self.params.get("branch", "")


class SipMessage:
    def __init__(self, *, method: str = "", uri: str = "", status: int = 0, reason: str = "",
                 headers: Optional[List[Tuple[str, str]]] = None, body: bytes = b""):
        self.method = method.upper()
        self.uri = uri
        self.status = status
        self.reason = reason
        self.headers: List[Tuple[str, str]] = list(headers or [])
        self.body = body

    @property
    def is_request(self) -> bool:
        return bool(self.method) and not self.status

    # -- headers ---------------------------------------------------------------
    def get_all(self, name: str) -> List[str]:
        key = canonical(name).lower()
        values: List[str] = []
        for header, value in self.headers:
            if header.lower() == key:
                values.extend(split_commas(value) if key in MULTI else [value.strip()])
        return values

    def get(self, name: str, default: str = "") -> str:
        values = self.get_all(name)
        return values[0] if values else default

    def set(self, name: str, value: str) -> None:
        self.remove(name)
        self.headers.append((canonical(name), value))

    def add(self, name: str, value: str, *, first: bool = False) -> None:
        if first:
            self.headers.insert(0, (canonical(name), value))
        else:
            self.headers.append((canonical(name), value))

    def remove(self, name: str) -> None:
        key = canonical(name).lower()
        self.headers = [(h, v) for h, v in self.headers if h.lower() != key]

    # -- convenience -------------------------------------------------------------
    @property
    def call_id(self) -> str:
        return self.get("Call-ID")

    @property
    def cseq(self) -> Tuple[int, str]:
        number_text, _, method = self.get("CSeq").strip().partition(" ")
        number = ascii_int(number_text)
        method = method.strip().upper()
        if number is None or not 0 <= number <= 0xFFFFFFFF or not method:
            raise SipParseError("bad CSeq")
        return number, method

    @property
    def from_addr(self) -> NameAddr:
        return NameAddr.parse(self.get("From"))

    @property
    def to_addr(self) -> NameAddr:
        return NameAddr.parse(self.get("To"))

    @property
    def top_via(self) -> Via:
        return Via.parse(self.get("Via"))

    def encode(self) -> bytes:
        if self.is_request:
            start = f"{self.method} {self.uri} SIP/2.0"
        else:
            start = f"SIP/2.0 {self.status} {self.reason or REASONS.get(self.status, 'Unknown')}"
        lines = [start]
        for name, value in self.headers:
            if name.lower() != "content-length":
                lines.append(f"{name}: {value}")
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + self.body

    def __repr__(self) -> str:
        first = f"{self.method} {self.uri}" if self.is_request else f"{self.status} {self.reason}"
        return f"<SipMessage {first} call-id={self.call_id}>"


def parse(data: bytes) -> SipMessage:
    if len(data) > MAX_MESSAGE_BYTES:
        raise SipParseError("message too large")
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = data.partition(b"\n\n")
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SipParseError("headers are not UTF-8") from exc
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise SipParseError("empty message")
    start = lines[0].strip()
    headers: List[Tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in " \t" and headers:  # folded continuation
            name, value = headers[-1]
            headers[-1] = (name, value + " " + line.strip())
            continue
        name, colon, value = line.partition(":")
        if not colon:
            raise SipParseError(f"bad header line {line!r}")
        headers.append((canonical(name), value.strip()))
    if start.startswith("SIP/2.0 "):
        parts = start.split(" ", 2)
        status = ascii_int(parts[1]) if len(parts) >= 2 else None
        if status is None or not 100 <= status <= 699:
            raise SipParseError(f"bad status line {start!r}")
        message = SipMessage(status=status, reason=parts[2] if len(parts) > 2 else "", headers=headers)
    else:
        parts = start.split(" ")
        if len(parts) != 3 or parts[2] != "SIP/2.0":
            raise SipParseError(f"bad request line {start!r}")
        message = SipMessage(method=parts[0], uri=parts[1], headers=headers)
    length_text = message.get("Content-Length")
    if length_text:
        length = ascii_int(length_text)
        if length is None:
            raise SipParseError("bad Content-Length")
        if length > len(body):
            raise SipParseError("truncated body")
        body = body[:length]
    message.body = body
    for required in ("Via", "From", "To", "Call-ID", "CSeq"):
        if not message.get(required):
            raise SipParseError(f"missing {required}")
    return message


def new_tag() -> str:
    return secrets.token_hex(6)


def new_branch() -> str:
    return MAGIC_COOKIE + secrets.token_hex(10)


def new_call_id(host: str) -> str:
    return f"{secrets.token_hex(12)}@{host}"


def stamp_via(value: str, source: Tuple[str, int]) -> str:
    """The top Via of a received request with ``received``/``rport`` filled in.

    RFC 3261 §18.2.1: ``received`` when the source address differs from sent-by; RFC 3581 §4: with an empty
    ``rport`` the server fills in the source port and also adds ``received``.
    """
    head, sep, param_text = value.partition(";")
    params: List[Tuple[str, str]] = []
    for item in param_text.split(";") if sep else []:
        item = item.strip()
        if item:
            key, eq, val = item.partition("=")
            params.append((key.strip(), val.strip() if eq else None))
    try:
        via = Via.parse(value)
        sent_host = via.host.strip("[]")
    except SipParseError:
        return value
    names = [key.lower() for key, _ in params]
    has_rport = "rport" in names
    out = []
    for key, val in params:
        lower = key.lower()
        if lower == "rport" and has_rport:
            out.append(f"rport={source[1]}")
        elif lower == "received":
            continue
        else:
            out.append(key if val is None else f"{key}={val}")
    if has_rport or sent_host != source[0]:
        out.insert(0 if not out else len(out), f"received={source[0]}")
    return head.strip() + "".join(";" + item for item in out)


def response_to(request: SipMessage, status: int, reason: str = "", *, to_tag: str = "",
                extra: Optional[List[Tuple[str, str]]] = None, body: bytes = b"",
                content_type: str = "", source: Optional[Tuple[str, int]] = None) -> SipMessage:
    """A response copying Via/From/To/Call-ID/CSeq as RFC 3261 §8.2.6 requires.

    ``source``: the address the request came from; it is stamped into the top Via (``received``/``rport``).
    """
    headers: List[Tuple[str, str]] = []
    for index, value in enumerate(request.get_all("Via")):
        headers.append(("Via", stamp_via(value, source) if index == 0 and source is not None else value))
    headers.append(("From", request.get("From")))
    to_value = request.get("To")
    if to_tag and ";tag=" not in to_value.replace(" ", "").lower():
        to_value = f"{to_value};tag={to_tag}"
    headers.append(("To", to_value))
    headers.append(("Call-ID", request.call_id))
    headers.append(("CSeq", request.get("CSeq")))
    if status >= 200 or status in (180, 183):
        for value in request.get_all("Record-Route"):
            headers.append(("Record-Route", value))
    headers.extend(extra or [])
    if body:
        headers.append(("Content-Type", content_type or "application/sdp"))
    return SipMessage(status=status, reason=reason or REASONS.get(status, ""), headers=headers, body=body)
