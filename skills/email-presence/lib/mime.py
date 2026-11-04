"""Small RFC 5322 helpers used by the email Presence companion."""
from __future__ import annotations

import hashlib
import html
import mimetypes
import re
from email import message_from_bytes
from email.header import decode_header, make_header
from email.errors import HeaderParseError, InvalidHeaderDefect, MessageError, NonPrintableDefect
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser
from email.policy import default
from email.utils import getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Any

_ADDRESS_PARSE_ERRORS = (ValueError, IndexError, AttributeError, TypeError, HeaderParseError)


def _wire_text(value: str, errors: str = "replace") -> str:
    # raw_items uses surrogateescape for octets above ASCII.
    return value.encode("utf-8", "surrogateescape").decode("utf-8", errors)


def decode_header_value(value: Any) -> str:
    if value is None:
        return ""
    # Unfold the source syntax before decoding encoded words; their decoded
    # content is not another header line to rewrite.
    source = re.sub(r"\r?\n(?=[ \t])", "", str(value))
    try:
        text = str(make_header(decode_header(source)))
    except (LookupError, UnicodeError, ValueError, MessageError):
        text = source
    # raw_items uses surrogateescape for 8-bit wire bytes. Recover valid UTF-8,
    # replace undecodable bytes for display, and retain the original MIME source.
    return _wire_text(text)


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


# email.headerregistry lazily constructs a Message-ID tree; a malformed value
# can raise rather than reporting a defect (notably on Python 3.10).
_HEADER_PARSE_ERRORS = (*_ADDRESS_PARSE_ERRORS, UnboundLocalError, RecursionError, OverflowError)


def usable_message_id(value: str | None) -> str:
    """Decode one usable identity, retaining the established <local-token> form."""
    if not value:
        return ""
    try:
        # Preserve UTF-8 IDs, but never deduplicate on lossy replacement text.
        parsed = default.header_fetch_parse("Message-ID", _wire_text(value, errors="strict"))
        if not any(isinstance(d, (InvalidHeaderDefect, NonPrintableDefect)) for d in parsed.defects):
            return str(parsed).strip()
    except _HEADER_PARSE_ERRORS:
        pass
    # Old mail and local clients can use a single unqualified atom. This is
    # distinct from trusting an arbitrary broken header as a deduplication key.
    token = str(value).strip()
    atom = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    return token if re.fullmatch(fr"<{atom}(?:\.{atom})*>", token) else ""


def _source_message_id(digest: str) -> str:
    return f"<sha256-{digest}@ouroboros.local>"


def prior_imap_capture(raw: bytes | None, message_id: str) -> tuple[str, str] | None:
    """Identify a 0.10.2 header key only when full MIME now supplies the identity."""
    if not raw or not message_id.startswith("<sha256-"):
        return None
    # Only the source headers are needed to reconstruct the previous parser's
    # actual value (e.g. an implicitly closed angle bracket), not the body.
    end = re.search(br"\r\n\r\n|\n\n|\r\r", raw)
    headers = BytesHeaderParser(policy=default).parsebytes(raw[:end.start()] if end else raw)
    try:
        old = str(headers.get("Message-ID") or "").strip()
    except _HEADER_PARSE_ERRORS:
        return None  # The old parser could not have admitted this message.
    if not old or old == message_id:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    return (old, digest) if message_id == _source_message_id(digest) else None


def _message_ids(value: Any) -> list[str]:
    return [item.strip() for item in re.findall(r"<[^>]+>", str(value or "")) if item.strip()]


def _addresses(value: Any) -> list[str]:
    return [addr for _, addr in getaddresses([decode_header_value(value)]) if addr]


class MailAddressError(ValueError):
    """A deterministic mailbox-address failure, distinct from transport/Host errors."""


def mailbox_addresses(value: Any) -> list[str]:
    """Parse complete mailbox lists before grouping/deduplication or envelope use."""
    values = value if isinstance(value, (list, tuple)) else [value]
    text = ", ".join(str(item).strip() for item in values if item is not None and str(item).strip())
    if not text:
        return []
    if "\r" in text or "\n" in text:
        raise MailAddressError("Mailbox addresses must be one RFC address list, without line breaks")
    try:
        message = EmailMessage()
        message['To'] = text
        header = message['To']
        parsed = header.addresses
    except _ADDRESS_PARSE_ERRORS as exc:
        raise MailAddressError("Invalid mailbox address list; use complete email addresses") from exc
    if (any(isinstance(defect, InvalidHeaderDefect) for defect in header.defects)
            or not parsed or any(not address.username or not address.domain for address in parsed)):
        raise MailAddressError("Invalid mailbox address list; use complete email addresses")
    return [str(address) for address in parsed]


def conversation_root(*, references=(), in_reply_to="", message_id=""):
    """The RFC root used by intake, preparation, queued bindings and delivery."""
    return str(next(iter(references or ()), "") or in_reply_to or message_id).strip()


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
    missing_id: str | None = None,
    invalid_id: str = "",
) -> dict[str, Any]:
    msg = message_from_bytes(raw, policy=default)
    def raw_header(name):
        return next((v for k, v in msg.raw_items() if k.lower() == name.lower()), None)

    def header(name):
        try:
            return msg.get(name)
        except _HEADER_PARSE_ERRORS:
            return _wire_text(raw_header(name) or "")

    raw_id = raw_header("Message-ID")
    message_id = usable_message_id(raw_id)
    if not message_id:
        # A metadata projection is not the original wire message. Its caller
        # can supply an empty ID and still return an exact provider message_ref.
        message_id = (invalid_id if raw_id and raw_id.strip() else "") or missing_id
        if message_id is None:
            message_id = _source_message_id(hashlib.sha256(raw).hexdigest())
    sender_name, sender_addr = parseaddr(decode_header_value(header("From")))
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
        "in_reply_to": str(header("In-Reply-To") or "").strip(),
        "references": _message_ids(header("References")),
        "subject": decode_header_value(msg.get("Subject")),
        "sender": sender_addr or decode_header_value(header("From")),
        "sender_name": sender_name,
        "recipients": _addresses(header("To")),
        "cc": _addresses(header("Cc")),
        "reply_to": _addresses(header("Reply-To")),
        "date": str(header("Date") or ""),
        "body": extract_body(msg, max_chars=max_body_chars),
        "attachments": attachments,
    }
    if not message_id:
        result["message_id_note"] = "No usable Message-ID in these headers; read the full message by message_ref."
    result["headers"] = {
        name.lower(): [decode_header_value(value) for key, value in msg.raw_items() if key.lower() == name.lower()]
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


def _mailbox_key(value):
    return (parseaddr(str(value))[1] or str(value)).strip().casefold()


def recipient_groups(to, cc=(), bcc=(), *, exclude=()):
    """Keep the first spelling and order, with visible To then Cc taking precedence."""
    seen = {_mailbox_key(value) for value in exclude}
    def group(values):
        result = []
        for value in mailbox_addresses(values):
            key = _mailbox_key(value)
            if key not in seen:
                result.append(value)
                seen.add(key)
        return result
    return group(to), group(cc), group(bcc)


def reply_all_recipients(message: dict[str, Any], mailbox=()) -> dict[str, list[str]]:
    """Expand visible source headers, excluding every sender identity of the selected account."""
    own = [mailbox] if isinstance(mailbox, str) else mailbox
    sender = message.get("sender") or ""
    primary = mailbox_addresses(message.get("reply_to") or sender)
    others = mailbox_addresses(message.get("recipients"))
    copies = mailbox_addresses(message.get("cc"))
    if sender and _mailbox_key(sender) in {_mailbox_key(address) for address in own}:
        primary.extend(others)  # Replying to our sent mail keeps its original To recipients together.
    else:
        copies = others + copies
    to, cc, _ = recipient_groups(primary, copies, exclude=own)
    if not to and cc:
        to.append(cc.pop(0))  # As before, a visible addressee can anchor a reply with no external sender.
    return {"to": to, "cc": cc}


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
