"""One plain-text message to one address over a certificate-verified TLS SMTP session.

Only implicit TLS (``ssl``, usually port 465) and ``starttls`` (usually 587) exist;
there is no plaintext mode. The certificate is checked against the system trust
store through ``ssl.create_default_context()``.

The send is split so the caller can re-check its admission right before the
message text is handed over: connect, TLS, login, MAIL FROM, RCPT TO, then
``recheck()``; only if it still answers True is DATA started, and once the server
has answered 354 ``recheck()`` runs again, immediately before the text is sent (a
server may hold its 354 for a long time). If that second check refuses or fails,
the connection is closed without another byte: no QUIT or RSET is written while the
server is reading message text, and a transaction without its final ``.`` line is
never delivered. A ``recheck()`` that raises counts as a refusal. Outcomes:

* ``accepted`` — the server answered 250 after the message; acceptance by the
  sender's mail server, not delivery to or reading by the recipient;
* ``not_sent`` — the message was never handed over, or the server refused it
  (a 4xx/5xx reply to the message itself means it was not accepted);
* ``aborted_before_data`` — the admission changed (or could not be confirmed)
  before any message text was sent;
* ``uncertain`` — the text was (or may have been) transmitted and no clear answer
  arrived; it may or may not be delivered and must not be resent blindly.

``test_connection`` is the owner's connectivity check: the same TLS, EHLO and login,
then QUIT — no MAIL, RCPT or DATA, so it names no recipient and sends nothing. It
proves that the server can be reached and the login works, not that mail is delivered.

EHLO names this client by the address literal of the connection's own local address
(RFC 5321 4.1.4), never by this computer's name: smtplib would otherwise look the name up
(``socket.getfqdn()``, a DNS query that runs after connecting and outside every timeout
here, seconds on some networks) and hand it to the mail server and its Received header.

Errors carry a short code and the numeric SMTP reply only — never server text,
credentials or the message.
"""

from __future__ import annotations

import email.policy
import ipaddress
import re
import smtplib
import socket
import ssl
import time
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple

MAX_SUBJECT = 150
MAX_BODY = 4000
MAX_NAME = 64
SECURITY_MODES = ("ssl", "starttls")
# The owner's connection test has a nominal total budget, not only a per-step timeout: in the
# server it holds this skill's handler scope, so the widget's own buttons wait while it runs.
# Each socket wait is cut to what is left of the budget (``_left``). Not bounded by it: the name
# lookup inside connect (the system resolver has no timeout here), and a server that keeps each
# single reply alive by trickling it byte by byte (every received byte restarts the socket wait).
TEST_TOTAL_SECONDS = 15.0

_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ADDRESS_RE = re.compile(rf"^{_LOCAL}@{_LABEL}(?:\.{_LABEL})+$")
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$|^\d{{1,3}}(?:\.\d{{1,3}}){{3}}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class MessageInvalid(ValueError):
    """A recipient, header or body that must not be sent."""


def clean_address(raw: object) -> str:
    """Exactly one plain ASCII address; anything list-like or header-like is refused."""
    text = str(raw or "").strip()
    if not text or len(text) > 254 or _CONTROL_RE.search(text) or any(c in text for c in ' ,;<>()"[]\\:'):
        raise MessageInvalid("must be exactly one plain email address")
    if not _ADDRESS_RE.match(text) or len(text.split("@", 1)[0]) > 64:
        raise MessageInvalid("must be exactly one plain email address")
    return text


def clean_name(raw: object) -> str:
    text = " ".join(str(raw or "").split())
    if not text or len(text) > MAX_NAME or _CONTROL_RE.search(text) or any(c in text for c in '<>"@,;\\'):
        raise MessageInvalid(f"must be 1-{MAX_NAME} characters without < > \" @ , ; or line breaks")
    return text


def clean_host(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if not text or len(text) > 253 or not _HOST_RE.match(text):
        raise MessageInvalid("must be a host name or IPv4 address")
    return text


def clean_subject(raw: object) -> str:
    text = str(raw or "").strip()
    if not text or len(text) > MAX_SUBJECT or _CONTROL_RE.search(text):
        raise MessageInvalid(f"subject must be one line of 1-{MAX_SUBJECT} characters")
    return text


def clean_body(raw: object) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = "".join(c for c in text if c in "\n\t" or not _CONTROL_RE.match(c))
    if not text or len(text) > MAX_BODY:
        raise MessageInvalid(f"body must be 1-{MAX_BODY} characters of plain text")
    return text


def build_message(*, from_addr: str, to_addr: str, to_name: str, subject: str, body: str) -> bytes:
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = Address(addr_spec=clean_address(from_addr))
    msg["To"] = Address(display_name=clean_name(to_name), addr_spec=clean_address(to_addr))
    msg["Subject"] = clean_subject(subject)
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=clean_address(from_addr).split("@", 1)[1])
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(clean_body(body), charset="utf-8", cte="quoted-printable")
    return msg.as_bytes()


def _result(state: str, code: str, smtp_code: Optional[int] = None) -> Dict[str, object]:
    return {"state": state, "code": code, "smtp_code": smtp_code}


def _admitted(recheck: Callable[[], bool]) -> Tuple[bool, str]:
    """The caller's admission check; an exception refuses (nothing was handed over yet)."""
    try:
        return (True, "") if recheck() else (False, "admission_changed")
    except Exception:
        return False, "recheck_failed"


def server_settings(host: object, port: object, security: object) -> Tuple[str, int, str]:
    clean = clean_host(host)
    number = int(port)  # TypeError/ValueError for a non-number
    if not 1 <= number <= 65535 or security not in SECURITY_MODES:
        raise MessageInvalid("mail server settings are incomplete")
    return clean, number, str(security)


def _left(timeout: float, deadline: Optional[float]) -> float:
    """The socket timeout for the next step: ``timeout``, cut to what remains before ``deadline``."""
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("the mail server did not answer in time")
    return min(timeout, remaining)


# Handed to smtplib's constructor so it never looks up this computer's name; replaced by the
# connection's own address before the first EHLO (and kept only if that cannot be read).
_EHLO_FALLBACK = "[127.0.0.1]"


def _ehlo_name(sock: Optional[socket.socket]) -> str:
    """The EHLO argument: the address literal of the connection's local end, for example
    ``[192.0.2.7]`` or ``[IPv6:2001:db8::7]``. No lookup of any kind."""
    try:
        address = ipaddress.ip_address(str(sock.getsockname()[0]).split("%", 1)[0])
    except (AttributeError, IndexError, OSError, TypeError, ValueError):
        return _EHLO_FALLBACK
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            return f"[IPv6:{address.compressed}]"
        address = address.ipv4_mapped
    return f"[{address}]"


def _open(host: str, port: int, security: str, username: str, password: str, timeout: float,
          context: ssl.SSLContext, at: Dict[str, object], deadline: Optional[float] = None) -> smtplib.SMTP:
    """Connect, TLS, EHLO and (when a username is set) login; ``at["stage"]`` follows along.

    The connection is stored in ``at`` as soon as it exists so the caller can close it. With a
    ``deadline`` (``time.monotonic()``), every step's socket timeout is cut to the time left.
    """
    def step(smtp: smtplib.SMTP) -> None:
        if deadline is not None and smtp.sock is not None:
            smtp.sock.settimeout(_left(timeout, deadline))

    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, local_hostname=_EHLO_FALLBACK, timeout=_left(timeout, deadline),
                                context=context)
    else:
        smtp = smtplib.SMTP(host, port, local_hostname=_EHLO_FALLBACK, timeout=_left(timeout, deadline))
    at["smtp"] = smtp
    smtp.local_hostname = _ehlo_name(smtp.sock)
    step(smtp)
    smtp.ehlo()
    if security == "starttls":
        if not smtp.has_extn("starttls"):
            raise _StartTlsMissing()
        at["stage"] = "tls"
        step(smtp)
        smtp.starttls(context=context)
        step(smtp)
        smtp.ehlo()
    at["stage"] = "login"
    if username:
        step(smtp)
        smtp.login(username, password)
    return smtp


class _StartTlsMissing(Exception):
    """The server offers no STARTTLS: never fall back to plaintext."""


def _failure(exc: BaseException, stage: str) -> Dict[str, object]:
    """The outcome of a failure before any message text was sent."""
    if isinstance(exc, _StartTlsMissing):
        return _result("not_sent", "starttls_unavailable")
    if isinstance(exc, ssl.SSLCertVerificationError):
        return _result("not_sent", "tls_certificate_rejected")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return _result("not_sent", "auth_failed", getattr(exc, "smtp_code", None))
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return _result("not_sent", "auth_unsupported")
    if isinstance(exc, (smtplib.SMTPException, ssl.SSLError, socket.timeout, OSError)):
        return _result("not_sent", f"{stage}_failed", getattr(exc, "smtp_code", None))
    return _result("not_sent", "unexpected_error")


def _close(smtp: Optional[smtplib.SMTP], *, polite: bool) -> None:
    """QUIT when the server is in command mode; otherwise (inside DATA) only drop the socket."""
    if smtp is None:
        return
    if polite:
        try:
            smtp.quit()
            return
        except (smtplib.SMTPException, OSError):
            pass
    try:
        smtp.close()
    except OSError:
        pass


def send_message(
    *,
    host: str,
    port: int,
    security: str,
    username: str,
    password: str,
    from_addr: str,
    to_addr: str,
    to_name: str,
    subject: str,
    body: str,
    recheck: Callable[[], bool],
    timeout: float = 20.0,
) -> Dict[str, object]:
    """Send one message; see the module docstring for the outcome states."""
    try:
        payload = build_message(from_addr=from_addr, to_addr=to_addr, to_name=to_name,
                                subject=subject, body=body)
        host, port, security = server_settings(host, port, security)
    except Exception:  # nothing has touched the network yet
        return _result("not_sent", "invalid_message")
    context = ssl.create_default_context()
    at: Dict[str, object] = {"stage": "connect", "smtp": None}
    in_data = False   # True between the 354 reply and the server's reply to the final "."
    try:
        smtp = _open(host, port, security, username, password, timeout, context, at)
        at["stage"] = "envelope"
        code, _ = smtp.mail(clean_address(from_addr))
        if code != 250:
            return _result("not_sent", "sender_refused", code)
        code, _ = smtp.rcpt(clean_address(to_addr))
        if code not in (250, 251):
            return _result("not_sent", "recipient_refused", code)
        ok, why = _admitted(recheck)
        if not ok:
            try:
                smtp.rset()
            except (smtplib.SMTPException, OSError):
                pass
            return _result("aborted_before_data", why)
        at["stage"] = "data_command"
        code, _ = smtp.docmd("DATA")
        if code != 354:
            return _result("not_sent", "data_refused", code)
        in_data = True
        # The 354 may have taken long: the owner may have checked in, disarmed, changed the
        # contact, re-armed or restarted meanwhile. Re-check right before the text leaves.
        ok, why = _admitted(recheck)
        if not ok:
            return _result("aborted_before_data", why + "_after_354")
        at["stage"] = "data"
        text = re.sub(rb"(?m)^\.", b"..", payload)
        if not text.endswith(b"\r\n"):
            text += b"\r\n"
        smtp.send(text + b".\r\n")
        code, _ = smtp.getreply()
        in_data = False
        if code == 250:
            return _result("accepted", "accepted", code)
        if 400 <= code < 600:
            return _result("not_sent", "message_refused", code)
        return _result("uncertain", "unexpected_reply", code)
    except Exception as exc:
        if at["stage"] == "data":
            return _result("uncertain", "connection_lost_during_data", getattr(exc, "smtp_code", None))
        return _failure(exc, str(at["stage"]))
    finally:
        _close(at["smtp"], polite=not in_data)


def test_connection(*, host: object, port: object, security: object, username: str, password: str,
                    timeout: float = 10.0, total: float = TEST_TOTAL_SECONDS) -> Dict[str, object]:
    """TLS, EHLO and login only, then QUIT: ``{"state": "ok"|"failed", "code", "smtp_code"}``.

    No MAIL, RCPT or DATA command is ever written, so nothing can be sent and no recipient
    is named. ``ok`` means "reachable with a verified certificate and the login worked".
    ``total`` is a nominal budget: every step's socket wait is cut to what is left of it, and
    no new step starts after it is spent (a final QUIT gets one more short wait). The resolver
    and a trickling multi-line reply can still run past it (see ``TEST_TOTAL_SECONDS``).
    """
    try:
        host, port, security = server_settings(host, port, security)
    except (MessageInvalid, TypeError, ValueError):
        return {"state": "failed", "code": "settings_invalid", "smtp_code": None}
    at: Dict[str, object] = {"stage": "connect", "smtp": None}
    deadline = time.monotonic() + total
    try:
        _open(host, port, security, username, password, timeout, ssl.create_default_context(), at, deadline)
        return {"state": "ok", "code": "logged_in" if username else "connected_no_login", "smtp_code": None}
    except Exception as exc:
        failed = _failure(exc, str(at["stage"]))
        return {"state": "failed", "code": failed["code"], "smtp_code": failed["smtp_code"]}
    finally:
        smtp = at["smtp"]
        if smtp is not None and getattr(smtp, "sock", None) is not None:
            try:
                smtp.sock.settimeout(2.0)
            except OSError:
                pass
        _close(smtp, polite=True)
