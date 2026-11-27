"""Small RFC 5322 helpers used by the email Presence companion."""
from __future__ import annotations

import hashlib
import html
import mimetypes
import re
from email import message_from_bytes
from email._header_value_parser import get_cfws, get_msg_id, get_quoted_string
from email.header import decode_header, make_header
from email.errors import HeaderParseError, InvalidHeaderDefect, MessageError, NonPrintableDefect
from email.headerregistry import Address
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser
from email.policy import SMTP, default
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


def _carried(part: Message, root: Message) -> bool:
    """Whether a non-root part is carried content rather than the sender's own text.

    A part with an attachment disposition or a filename, whatever its declared disposition, and every
    message/* part (an attached or forwarded message, a delivery or read report) belongs to an attachment.
    The root is never carried: a single-part message is its own body, and a top-level message/* is entered.
    """
    return part is not root and (part.get_content_disposition() == "attachment" or bool(part.get_filename())
                                 or part.get_content_maintype() == "message")


def _content_parts(message: Message):
    """Yield the message's own leaf parts and its carried parts in order, never entering a carried part."""
    def walk(node):
        if node is message and node.get_content_maintype() == "message" and node.is_multipart():
            for inner in node.get_payload():
                yield from walk(inner)
        elif node.is_multipart() and not _carried(node, message):
            for part in node.get_payload():
                yield from walk(part)
        else:
            yield node
    yield from walk(message)


_TEXT_KINDS = ("text/plain", "text/html")


def _body_parts(message: Message) -> list[Message]:
    """The sender's own text parts: the message's un-named text, never an attachment or carried message.

    Only when the message has no non-blank un-named plain or HTML text of its own is a named inline plain or
    HTML part at its own level read as the body (a receipt sent as a lone ``receipt.html``). Parts with an
    attachment disposition, and anything inside an attached message or attachment container, never are.
    """
    own = [part for part in _content_parts(message)
           if not part.is_multipart() and part.get_content_type().lower() in _TEXT_KINDS]
    ordinary = [part for part in own if not _carried(part, message)]
    if any(_readable_text(part) for part in ordinary):
        return ordinary
    fallback = [part for part in own if part.get_content_disposition() != "attachment"]
    return fallback if any(_readable_text(part) for part in fallback) else ordinary


def _readable_text(part: Message) -> str:
    """The text a reader sees in one plain or HTML part, stripped; "" for a blank or empty-markup part."""
    text = _plain_from_part(part)
    return (_html_to_text(text) if part.get_content_type().lower() == "text/html" else text).strip()


def _sender_text(message: Message) -> tuple[str, list[str]]:
    parts = _body_parts(message)
    # The first non-blank plain part, else the first non-blank HTML part: a blank part never hides later text.
    plain = next((text for part in parts if part.get_content_type().lower() == "text/plain"
                  for text in [_readable_text(part)] if text), "")
    html_parts = [_plain_from_part(part) for part in parts if part.get_content_type().lower() == "text/html"]
    return plain or next((text for value in html_parts for text in [_html_to_text(value)] if text), ""), html_parts


def extract_body(message: Message, max_chars: int | None = 100_000) -> str:
    text, _ = _sender_text(message)
    return text if max_chars is None or len(text) <= max_chars else text[:max_chars] + f"\n[Body truncated: {len(text)} characters total]"


# Attached mail whose own files are listed as files. Delivery and read reports (message/delivery-status,
# message/disposition-notification) carry header blocks, not files, and stay single unavailable entries.
_ATTACHED_MESSAGES = frozenset({"message/rfc822", "message/global"})


def _safe_header(message: Message, name: str) -> str:
    try:
        return decode_header_value(message.get(name))
    except _ADDRESS_PARSE_ERRORS + (UnboundLocalError, RecursionError, OverflowError):
        return _wire_text(next((v for k, v in message.raw_items() if k.lower() == name.lower()), "") or "")


def _container(part: Message, inner: Message | None = None) -> dict[str, str]:
    """Compact provenance of one carrying container: its MIME type, name and, for mail, whose mail it is."""
    item = {"mime_type": part.get_content_type()}
    name = decode_header_value(part.get_filename() or "")
    if name:
        item["file_name"] = Path(name).name
    if inner is not None:
        for header in ("From", "Subject"):
            value = _safe_header(inner, header)
            if value:
                item[header.lower()] = value
    return item


def _file_parts(message: Message, carrier: tuple | None = None):
    """Yield ``(part, carrier)`` for every file the message carries, in MIME order.

    Each carried part is listed once, as before. An attached message (message/rfc822, message/global) is
    then read as a message: its own files follow it. Every leaf inside an attachment-marked multipart is
    a file of that attachment. ``carrier`` is ``(container part, its provenance, depth)`` for the nearest
    container a nested file came from, or None at the top level. The parser stores its provenance once on
    the container descriptor and links each child by index, without repeating headers across depth or fanout.
    """
    for part in _content_parts(message):
        if part is message:
            # A single-part message with an attachment disposition or filename is listed and remains its own body.
            if not part.is_multipart() and (part.get_content_disposition() == "attachment" or part.get_filename()):
                yield part, carrier
            continue
        if not _carried(part, message):
            continue
        yield part, carrier
        depth = (carrier[2] if carrier else 0) + 1
        if part.get_content_type().lower() in _ATTACHED_MESSAGES and part.is_multipart():
            inner = part.get_payload()[0]
            yield from _file_parts(inner, (part, _container(part, inner), depth))
        elif part.is_multipart() and part.get_content_maintype() != "message":
            yield from _attachment_leaves(part, (part, _container(part), depth))


def _attachment_leaves(container: Message, carrier: tuple):
    for part in container.get_payload():
        if part.get_content_type().lower() in _ATTACHED_MESSAGES and part.is_multipart():
            inner = part.get_payload()[0]
            yield part, carrier
            yield from _file_parts(inner, (part, _container(part, inner), carrier[2] + 1))
        elif part.is_multipart() and part.get_content_maintype() != "message":
            yield from _attachment_leaves(part, carrier)
        else:
            yield part, carrier


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


_ENCODED_WORDS = re.compile(r'=\?[^?\s]*\?[qQbB]\?[^?\s]*\?=(?:\s*=\?[^?\s]*\?[qQbB]\?[^?\s]*\?=)*')


def _decoded_words(run: str) -> str:
    # The intake header decoder, applied to one run of adjacent encoded words only.
    try:
        return str(default.header_fetch_parse('References', run))
    except _HEADER_PARSE_ERRORS:
        return run


def _message_ids(value: Any, *, encoded_words: bool = False) -> list[str]:
    """Read msg-id tokens, never brackets inside CFWS or a quoted phrase.

    Use the same grammar as Message-ID, including obsolete and UTF-8 forms. Recovery from unrelated text is
    conservative: an unterminated comment/quote consumes the remainder, never exposes its contents as ancestry.
    ``encoded_words`` reads a raw threading header: an encoded word outside comments and quoted strings is decoded
    as intake's header decoder decodes it; one inside them stays comment text and cannot become ancestry.
    """
    remaining, result, decoded = _wire_text(str(value or "")), [], 0
    while remaining:
        previous = remaining
        try:
            run = _ENCODED_WORDS.match(remaining) if encoded_words and not decoded else None
            if run:
                text = _decoded_words(run.group())
                # Decoded text is read once, in place, like a whole-header decode; it is never decoded again.
                remaining, decoded = text + remaining[run.end():], len(text) + 1
                continue
            if remaining[0] in ' \t\r\n(':
                _, remaining = get_cfws(remaining)
            elif remaining[0] == '"':
                _, remaining = get_quoted_string(remaining)
            elif remaining[0] == '<':
                parsed, remaining = get_msg_id(remaining)
                token = ''.join(str(part) for part in parsed if part.token_type != 'cfws')
                if usable_message_id(token):
                    result.append(token)
            else:
                remaining = remaining[1:]
        except _HEADER_PARSE_ERRORS:
            break
        if remaining == previous:  # a damaged header must not stall intake
            remaining = remaining[1:]
        decoded = max(0, decoded - (len(previous) - len(remaining)))
    return result


def thread_fields(references: Any, in_reply_to: Any) -> dict[str, Any]:
    """Intake's ``references`` and ``in_reply_to`` from raw threading headers; retained syntax is read the same way.

    In-Reply-To keeps intake's decoded text, which existing Place and queue keys use, unless decoding inside a
    comment or quoted string changed its msg-id tokens; then only the actual tokens remain."""
    try:
        text = str(default.header_fetch_parse('In-Reply-To', in_reply_to) or '').strip() if in_reply_to else ''
    except _HEADER_PARSE_ERRORS:
        text = _wire_text(str(in_reply_to)).strip()
    tokens = _message_ids(in_reply_to, encoded_words=True)
    if _message_ids(text) != tokens:
        text = ' '.join(tokens)
    return {'references': _message_ids(references, encoded_words=True), 'in_reply_to': text}


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
    """The RFC root used by intake, preparation, queued bindings and delivery.

    Existing Place and queue keys depend on this exact value, including a source whose only identity is a raw
    multi-ID In-Reply-To string. Reply lookup compares canonical tokens; the chain and historical ID come from
    the verified source without rewriting its key."""
    return str(next(iter(references or ()), "") or in_reply_to or message_id).strip()


def canonical_message_id(value: Any) -> str:
    """The single usable ``<id>`` token a caller's Message-ID names, or "" when it is not exactly one.

    Lookup compares this token on both sides; the verified source retains its stored header and conversation."""
    if not isinstance(value, str):
        return ""
    if usable := usable_message_id(value):
        # Use the same RFC parser as intake: angle brackets inside a legal comment are not another ID.
        parsed = default.header_fetch_parse("Message-ID", _wire_text(usable, errors="strict"))
        identifier = parsed._parse_tree[0]
        if identifier.token_type == 'msg-id':
            return ''.join(str(part) for part in identifier if part.token_type != 'cfws')
    tokens = _message_ids(value)
    if len(tokens) != 1:
        return ""
    token = usable_message_id(tokens[0])
    return token if token == tokens[0] else ""


def reply_authority(message) -> tuple:
    """What a reply to ``message`` inherits: its identity, conversation chain, envelope and provider thread.
    Copies of one Message-ID are interchangeable reply sources only when these are equal."""
    keys = ('message_id', 'references', 'in_reply_to', 'sender', 'reply_to', 'recipients', 'cc', 'provider_thread_id')
    return tuple(message.get(key) or ([] if key in ('references', 'reply_to', 'recipients', 'cc') else '')
                 for key in keys)


def _written_reply_id(in_reply_to: str) -> str:
    """The In-Reply-To a reply writes: the source's single canonical token, else (a legacy multi-ID value) as stored."""
    return canonical_message_id(in_reply_to) or str(in_reply_to)


class AmbiguousReplySource(ValueError):
    """One Message-ID names messages with different reply authority; a lookup by that ID alone cannot choose."""


def unique_reply_source(messages, message_id):
    """Match the canonical token without rewriting stored identities or choosing an ambiguous authority."""
    token = canonical_message_id(message_id)
    matches = [m for m in messages if token and canonical_message_id(m.get('message_id')) == token]
    # Copies in several folders are harmless only when they carry the same reply authority and envelope.
    if matches and any(reply_authority(m) != reply_authority(matches[0]) for m in matches[1:]):
        raise AmbiguousReplySource('Ambiguous reply source: this Message-ID names different messages in the mailbox')
    return matches[0] if matches else None


def emitted_conversation(in_reply_to, references, message_id=''):
    """Parse the actual delivery builder's headers with intake, including legacy multi-ID chains."""
    built = build_message(sender='check@example.invalid', recipients=['check@example.invalid'], subject='', body='',
                          in_reply_to=in_reply_to, references=references, message_id=message_id or '<check@example.invalid>')
    parsed = parse_message(built.as_bytes(policy=SMTP), folder='', uid=0)
    return conversation_root(references=parsed['references'], in_reply_to=parsed['in_reply_to'],
                             message_id=parsed['message_id'])


def reply_chain(source: dict[str, Any]) -> list[str]:
    """The verified source's actual ancestry, using retained syntax when an older projection lost CFWS."""
    from .retained import source_thread_fields
    fields = source_thread_fields(source)
    if fields is not None:
        return list(fields['references'] or ([fields['in_reply_to']] if fields['in_reply_to'] else []))
    return list(source.get("references") or ([source["in_reply_to"]] if source.get("in_reply_to") else []))


def wire_thread_problem(in_reply_to: str, references) -> str:
    """Write the reply headers with the delivery builder, parse them with the intake reader, and compare.

    The parsed In-Reply-To must be ``in_reply_to`` and the parsed References tokens must be the chain's own tokens
    in order (build_message appends In-Reply-To when the chain lacks it). Roots are not compared literally: a
    legacy source keeps its existing key. Callers must independently check emitted_conversation as well.
    Returns "" when the wire reads back exactly."""
    chain = [str(item).strip() for item in references or () if str(item).strip()]
    try:
        built = build_message(sender="check@example.invalid", recipients=["check@example.invalid"], subject="",
                              body="", in_reply_to=in_reply_to, references=chain, message_id="<check@example.invalid>")
        parsed = parse_message(built.as_bytes(policy=SMTP), folder="", uid=0)
    except _HEADER_WRITE_ERRORS:
        return "The reply headers cannot be written as mail header text"
    written = _written_reply_id(in_reply_to) if in_reply_to else ""
    expected = _message_ids(" ".join(chain + ([written] if written and written not in chain else [])))
    if parsed["in_reply_to"] != written or parsed["references"] != expected:
        return "The written reply headers would not read back as the verified source's conversation"
    return ""


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
        payload = part.get_payload(decode=True)
        if payload is None:
            # An attached message, report or carried container has no single decodable payload; re-serializing
            # it would not be the sender's bytes. Report it unavailable; the retained raw source still holds it exactly.
            item.update(filename=filename, content_type=content_type, content_disposition=disposition,
                        stage_error="unsupported_mime_type", error="unsupported_mime_type")
            return item
        payload = payload or b""
        item["content_available"] = bool(payload)
        # Internal hand-off to immutable staging. Removed before persistence.
        item.update(filename=filename, content_type=content_type,
                    content_disposition=disposition, content_id=str(part.get("Content-ID") or "").strip(),
                    size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        item["data"] = payload
    return item


_NAMED_FIELDS = ("Cc", "To", "From", "Reply-To")  # display_names_of precedence for Reply All: the first spelling wins


def header_mailboxes(field: str, value: Any) -> list[tuple[str, str]]:
    """(display name, bare address) of each complete mailbox in one address header, parsed from its raw RFC 5322
    syntax with the MIME reader's header model before any display name is decoded. An RFC 2047 encoded word decodes
    only inside its own name, so an encoded comma or "@" there never becomes another address, as it would if the
    decoded text were split. ``value`` is raw header text or an already parsed header. A damaged header names nobody."""
    try:
        header = default.header_fetch_parse(field, value)
        if any(isinstance(defect, InvalidHeaderDefect) for defect in getattr(header, "defects", ())):
            return []
        mailboxes = [(str(address.display_name or "").strip(), address.addr_spec)
                     for address in getattr(header, "addresses", ())
                     if address.addr_spec and address.username and address.domain]
    except _HEADER_PARSE_ERRORS:
        return []
    # 8-bit wire octets arrive surrogate-escaped; read them as the decoded headers text does.
    return [(_wire_text(name), _wire_text(spec)) for name, spec in mailboxes]


def address_names_of(values_of) -> dict[str, str]:
    """Display names of the Cc/To/From/Reply-To mailboxes, keyed by the casefolded bare address, from
    ``values_of(field)``: that field's raw or parsed headers. Names come from the raw syntax (header_mailboxes),
    so an encoded "Doe, John" stays one name; the decoded ``headers`` text would read that comma as a separator."""
    names: dict[str, str] = {}
    for field in _NAMED_FIELDS:
        for header in values_of(field):
            for name, spec in header_mailboxes(field, header):
                if name:
                    names.setdefault(spec.casefold(), name)
    return names


def _address_names(message: Message) -> dict[str, str]:
    def values_of(field):
        try:
            return message.get_all(field) or []
        except _HEADER_PARSE_ERRORS:
            return []
    return address_names_of(values_of)


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
    attachments, listed, locator = [], {}, {}
    for part, carrier in _file_parts(msg):
        item = _attachment_metadata(part, include_data=include_attachment_data)
        if carrier:
            container, provenance, depth = carrier
            if not locator:
                # Each part's depth-first position in the raw MIME tree (0 is the message itself): a compact locator.
                locator = {id(node): position for position, node in enumerate(msg.walk())}
            # The nearest carrying container; its own entry, at container_index, names the next one out.
            attachments[listed[id(container)]]["container"] = provenance
            item.update(container_index=listed[id(container)], depth=depth,
                        mime_part=locator.get(id(part), 0))
        listed[id(part)] = len(attachments)
        attachments.append(item)
    body, html_parts = _sender_text(msg)
    if max_body_chars is not None and len(body) > max_body_chars:
        body = body[:max_body_chars] + f"\n[Body truncated: {len(body)} characters total]"
    result: dict[str, Any] = {
        "folder": folder,
        "uid": int(uid),
        "message_id": message_id,
        **thread_fields(raw_header("References"), raw_header("In-Reply-To")),
        "thread_headers": {"references": str(raw_header("References") or ""),
                           "in_reply_to": str(raw_header("In-Reply-To") or "")},
        "subject": decode_header_value(msg.get("Subject")),
        "sender": sender_addr or decode_header_value(header("From")),
        "sender_name": sender_name,
        "recipients": _addresses(header("To")),
        "cc": _addresses(header("Cc")),
        "reply_to": _addresses(header("Reply-To")),
        "date": str(header("Date") or ""),
        "body": body,
        "attachments": attachments,
    }
    if not message_id:
        result["message_id_note"] = "No usable Message-ID in these headers; read the full message by message_ref."
    result["headers"] = {
        name.lower(): [decode_header_value(value) for key, value in msg.raw_items() if key.lower() == name.lower()]
        for name in ("From", "To", "Cc", "Reply-To", "List-Id", "List-Unsubscribe", "Precedence", "Auto-Submitted")
        if name in msg or name in ("From", "To", "Cc", "Reply-To")
    }
    result["address_names"] = _address_names(msg)
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


def envelope_groups(to, cc=(), bcc=(), *, exclude=()):
    """The bare addresses each visible group delivers to: deduplicated To, then Cc, then Bcc, in the same
    parseaddr form that OutboxItem.envelope_recipients hands to SMTP, Gmail and Exchange."""
    groups = []
    for group in recipient_groups(to, cc, bcc, exclude=exclude):
        bare = [parseaddr(value)[1] for value in group]
        if any(not value for value in bare):
            raise MailAddressError("Invalid mailbox address list; use complete email addresses")
        groups.append(bare)
    return tuple(groups)


def display_names_of(*values: Any) -> dict[str, str]:
    """Display names spelled in mailbox lists, keyed by the casefolded bare address; the first spelling wins."""
    names: dict[str, str] = {}
    for value in values:
        try:
            mailboxes = mailbox_addresses(value) if value else []
        except MailAddressError:
            continue  # A damaged source header names nobody; its recipients were validated separately.
        for mailbox in mailboxes:
            name, address = parseaddr(mailbox)
            if name.strip() and address:
                names.setdefault(address.casefold(), name.strip())
    return names


_RECIPIENT_FIELDS = ("To", "Cc", "Bcc")
_HEADER_WRITE_ERRORS = (*_HEADER_PARSE_ERRORS, MessageError, UnicodeError)


def _wire_header_groups(groups) -> tuple[list[tuple[str, str]], ...]:
    """(display name, bare address) pairs of mailbox groups after writing them as SMTP headers and parsing them again.

    Delivery writes the queued header spellings with this same header model and policy, so this is what the
    recipient's server reads. Raises MailAddressError when the header cannot be written or read back cleanly.
    """
    headers = EmailMessage()
    try:
        for field, group in zip(_RECIPIENT_FIELDS, groups):
            if group:
                headers[field] = ", ".join(group)
        wire = message_from_bytes(headers.as_bytes(policy=SMTP), policy=default)
        result = []
        for field in _RECIPIENT_FIELDS:
            header = wire.get(field)
            if header is not None and any(isinstance(d, InvalidHeaderDefect) for d in header.defects):
                raise MailAddressError("A recipient header would not read back as written")
            result.append([] if header is None else [(a.display_name, parseaddr(str(Address(addr_spec=a.addr_spec)))[1])
                                                     for a in header.addresses])
        return tuple(result)
    except _HEADER_WRITE_ERRORS as exc:
        raise MailAddressError("A recipient header cannot be written as mail header text") from exc


def _display_name_problem(address: str, name: Any) -> str:
    """Why ``name`` cannot be the header display name of ``address``, or "" when it round-trips exactly."""
    if (not isinstance(name, str) or not name.strip()
            or any(ord(char) < 32 or ord(char) == 127 or char in "\x85\u2028\u2029" for char in name)):
        return "Each display name must be one line of text, non-empty and without control characters"
    try:
        written = _wire_header_groups([named_mailboxes([address], {address: name.strip()})])[0]
    except MailAddressError:
        written = None
    if written != [(" ".join(name.split()), address)] and written != [(name.strip(), address)]:
        # e.g. text spelled as an RFC 2047 encoded word, which a mail reader would decode into other text.
        return f"The display name for {address} would not read back unchanged from a mail header; use plain text"
    return ""


def checked_display_names(value: Any, groups) -> dict[str, str]:
    """Validate a caller's display_names: header text for listed recipients only, never a recipient itself.

    A name is any one line of text that reads back unchanged from the written header; the header encoder quotes,
    folds and RFC 2047-encodes it as needed, so no separate length or character-set limit applies.
    """
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("display_names must be an object mapping a listed recipient address to its display name")
    listed = {address.casefold(): address for group in groups for address in group}
    names = {}
    for address, name in value.items():
        if not isinstance(address, str) or address.casefold() not in listed:
            raise ValueError("display_names may only name a recipient listed in to, cc or bcc")
        problem = _display_name_problem(listed[address.casefold()], name)
        if problem:
            raise ValueError(problem)
        names[address.casefold()] = name.strip()
    return names


def usable_display_names(names: dict[str, str]) -> dict[str, str]:
    """The names, read from source headers or named input, that a header can carry unchanged; others are left out."""
    return {address: name for address, name in names.items() if not _display_name_problem(address, name)}


def named_mailboxes(group, names: dict[str, str]) -> list[str]:
    """The header spelling of bare addresses: ``"Name, Quoted" <addr>`` where a display name is known."""
    result, folded = [], {str(key).casefold(): value for key, value in names.items()}
    for address in group:
        name = folded.get(str(address).casefold())
        try:
            result.append(str(Address(display_name=name, addr_spec=address)) if name else str(address))
        except _ADDRESS_PARSE_ERRORS as exc:
            raise MailAddressError(f"The display name for {address} cannot be written in a mail header") from exc
    return result


def named_recipient_groups(groups, names: dict[str, str]) -> tuple[list[str], ...]:
    """Header spellings of the bare ``groups`` with their display names, proven to deliver to exactly ``groups``.

    Both the queued spellings (what the outbox envelope parses) and the serialized headers (what the recipient's
    server parses) must yield the same bare addresses in the same groups; a name can never add or change one.
    """
    bare = tuple(list(group) for group in groups)
    named = tuple(named_mailboxes(group, names) for group in bare)
    # Without a name the spellings are the bare addresses themselves, exactly as an unnamed send queued them.
    try:
        written = (tuple([address for _name, address in group] for group in _wire_header_groups(named))
                   if named != bare else bare)
    except MailAddressError:
        written = None
    if envelope_groups(*named) != bare or written != bare:
        raise MailAddressError("display_names would change the recipients when written as mail headers; "
                               "use plain-text names or omit them")
    return named


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
    to = ", ".join(str(item) for item in recipients if str(item).strip())
    if to:  # a mail with only Cc or Bcc recipients has no To header, rather than an empty one
        message["To"] = to
    if cc:
        message["Cc"] = ", ".join(str(item) for item in cc if str(item).strip())
    if include_bcc_header and bcc:
        message["Bcc"] = ", ".join(str(item) for item in bcc if str(item).strip())
    message["Subject"] = subject or "(no subject)"
    message["Message-ID"] = message_id or make_msgid()
    chain = [str(item).strip() for item in references if str(item).strip()]
    if in_reply_to:
        # Name the source by its canonical token: a comment in its stored spelling, which may itself spell another
        # ID, is not ancestry. Stored sources, queue rows and Place keys keep their spelling; only the wire changes.
        in_reply_to = _written_reply_id(in_reply_to)
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
        # body itself is the HTML; its plain alternative is rendered from it.
        html_value, plain_value = plain_value, ""
    if html_value:
        # An explicit body is always the plain alternative, never silently replaced by text rendered from html_body.
        message.set_content(plain_value or _html_to_text(html_value))
        message.add_alternative(html_value, subtype="html")
    else:
        message.set_content(plain_value)
    _add_attachments(message, attachments)
    return message
