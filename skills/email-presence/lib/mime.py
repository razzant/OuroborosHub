"""Small RFC 5322 helpers used by the email Presence companion."""
from __future__ import annotations

import hashlib
import html
import mimetypes
import re
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Any


def decode_header_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except (LookupError, UnicodeError, ValueError):
        return str(value)


def _plain_from_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return str(raw or "") if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(re.sub(r"[ \t]+", " ", value)).strip()


def extract_body(message: Message, max_chars: int | None = 100_000) -> str:
    plain = ""
    html_body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            kind = part.get_content_type().lower()
            value = _plain_from_part(part)
            if kind == "text/plain" and not plain:
                plain = value
            elif kind == "text/html" and not html_body:
                html_body = value
    else:
        kind = message.get_content_type().lower()
        value = _plain_from_part(message)
        plain = value if kind == "text/plain" else ""
        html_body = value if kind == "text/html" else ""
    text = plain.strip() or _html_to_text(html_body)
    return text if max_chars is None or len(text) <= max_chars else text[:max_chars] + f"\n[Body truncated: {len(text)} characters total]"


def _message_ids(value: Any) -> list[str]:
    return [item.strip() for item in re.findall(r"<[^>]+>", str(value or "")) if item.strip()]


def _addresses(value: Any) -> list[str]:
    return [addr for _, addr in getaddresses([decode_header_value(value)]) if addr]


def _attachment_metadata(part: Message, *, include_data: bool = False) -> dict[str, Any]:
    filename = decode_header_value(part.get_filename() or "")
    filename = Path(filename).name if filename else "attachment"
    content_type = part.get_content_type() or "application/octet-stream"
    disposition = str(part.get_content_disposition() or "")
    item: dict[str, Any] = {
        # Keep the established Presence metadata shape for ordinary reads.
        "file_name": filename,
        "mime_type": content_type,
        "disposition": disposition,
        "content_available": False,
    }
    if include_data:
        payload = part.get_payload(decode=True) or b""
        item["content_available"] = bool(payload)
        # Internal hand-off to immutable staging. Removed before persistence.
        item.update(filename=filename, content_type=content_type,
                    content_disposition=disposition, content_id=str(part.get("Content-ID") or "").strip(),
                    size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        item["data"] = payload
    return item


def parse_message(
    raw: bytes,
    *,
    folder: str,
    uid: int,
    max_body_chars: int = 100_000,
    include_attachment_data: bool = False,
) -> dict[str, Any]:
    msg = message_from_bytes(raw, policy=default)
    message_id = str(msg.get("Message-ID") or "").strip() or f"<sha256-{hashlib.sha256(raw).hexdigest()}@ouroboros.local>"
    sender_name, sender_addr = parseaddr(decode_header_value(msg.get("From")))
    attachments = []
    for part in msg.walk():
        if part.is_multipart() and not (part.get_filename() or part.get_content_disposition() == "attachment"):
            continue
        disposition = str(part.get_content_disposition() or "").lower()
        if disposition == "attachment" or part.get_filename():
            attachments.append(_attachment_metadata(part, include_data=include_attachment_data))
    result: dict[str, Any] = {
        "folder": folder,
        "uid": int(uid),
        "message_id": message_id,
        "in_reply_to": str(msg.get("In-Reply-To") or "").strip(),
        "references": _message_ids(msg.get("References")),
        "subject": decode_header_value(msg.get("Subject")),
        "sender": sender_addr or decode_header_value(msg.get("From")),
        "sender_name": sender_name,
        "recipients": _addresses(msg.get("To")),
        "cc": _addresses(msg.get("Cc")),
        "reply_to": _addresses(msg.get("Reply-To")),
        "date": str(msg.get("Date") or ""),
        "body": extract_body(msg, max_chars=max_body_chars),
        "attachments": attachments,
    }
    result["headers"] = {
        name.lower(): [decode_header_value(value) for value in msg.get_all(name, [])]
        for name in ("From", "To", "Cc", "Reply-To", "List-Id", "List-Unsubscribe", "Precedence", "Auto-Submitted")
        if name in msg or name in ("From", "To", "Cc", "Reply-To")
    }
    html_parts = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type().lower() == "text/html":
            html_parts.append(_plain_from_part(part))
    if html_parts:
        result["body_html"] = "\n".join(html_parts)
    if include_attachment_data:
        # Kept internal until EmailStore atomically stages it. This lets the
        # tool and companion expose a durable raw RFC822 artifact when a body
        # or attachment is clipped, without putting bytes into JSON/SQLite.
        result["_raw_source"] = raw
    return result


def reply_all_recipients(message: dict[str, Any], mailbox: str = "") -> dict[str, list[str]]:
    """Return RFC-style Reply-All groups while excluding the sending mailbox."""
    own = (parseaddr(str(mailbox or ""))[1] or str(mailbox or "")).strip().casefold()
    reply_targets = list(message.get("reply_to") or [])
    if not reply_targets:
        sender = str(message.get("sender") or "").strip()
        if sender:
            reply_targets = [sender]
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        address = str(value or "").strip()
        key = (parseaddr(address)[1] or address).casefold()
        if not address or key == own or key in seen:
            return
        seen.add(key)
        ordered.append(address)

    for value in reply_targets:
        add(value)
    to_count = len(ordered)
    for value in tuple(message.get("recipients") or ()) + tuple(message.get("cc") or ()):
        add(value)
    if to_count == 0 and ordered:
        to_count = 1
    return {"to": ordered[:to_count], "cc": ordered[to_count:]}


def _attachment_bytes(item: dict[str, Any]) -> bytes:
    data = item.get("data")
    if isinstance(data, bytes):
        return data
    path = str(item.get("path") or "")
    if path:
        return Path(path).read_bytes()
    if isinstance(data, str):
        return data.encode()
    return b""


def _add_attachments(message: EmailMessage, attachments: Any) -> None:
    for index, raw in enumerate(attachments or ()):
        item = dict(raw) if isinstance(raw, dict) else {"path": str(raw)}
        data = _attachment_bytes(item)
        filename = Path(str(item.get("filename") or item.get("name") or f"attachment-{index}")).name
        content_type = str(item.get("content_type") or item.get("mimetype") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        maintype, _, subtype = content_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


def build_message(
    *,
    sender: str,
    recipients: list[str],
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: list[str] | tuple[str, ...] = (),
    message_id: str = "",
    cc: list[str] | tuple[str, ...] = (),
    bcc: list[str] | tuple[str, ...] = (),
    include_bcc_header: bool = False,
    html_body: str = "",
    body_type: str = "plain",
    attachments: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> EmailMessage:
    """Build a MIME message; Bcc is an SMTP envelope only and never a header."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(str(item) for item in recipients if str(item).strip())
    if cc:
        message["Cc"] = ", ".join(str(item) for item in cc if str(item).strip())
    if include_bcc_header and bcc:
        message["Bcc"] = ", ".join(str(item) for item in bcc if str(item).strip())
    message["Subject"] = subject or "(no subject)"
    message["Message-ID"] = message_id or make_msgid()
    chain = [str(item).strip() for item in references if str(item).strip()]
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        if in_reply_to not in chain:
            chain.append(in_reply_to)
    if chain:
        message["References"] = " ".join(chain)
    kind = str(body_type or "plain").lower()
    if kind not in {"plain", "html"}:
        raise ValueError("body_type must be 'plain' or 'html'")
    html_value = str(html_body or "")
    plain_value = str(body or "")
    if kind == "html" and not html_value:
        html_value = plain_value
    if html_value:
        message.set_content(_html_to_text(html_value) if kind == "html" else (plain_value or _html_to_text(html_value)))
        message.add_alternative(html_value, subtype="html")
    else:
        message.set_content(plain_value)
    _add_attachments(message, attachments)
    return message
