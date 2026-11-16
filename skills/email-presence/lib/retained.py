"""Interpret retained source syntax without migrating identities or accepted envelopes."""
import hashlib
from email.parser import BytesHeaderParser
from email.policy import default
from pathlib import Path

from .mime import (MailAddressError, _message_ids, address_names_of, envelope_groups, header_mailboxes, mailbox_addresses,
                   thread_fields)


class RetainedSourceError(MailAddressError):
    """A retained source cannot safely supply reply headers or recipients."""


class UndecidableThreadSyntax(RetainedSourceError):
    """Only decoded threading text survived, and decoding may have moved a comment or quote boundary."""


def source_thread_fields(source):
    """The source's actual ``references``/``in_reply_to``, read from retained syntax exactly as intake reads it."""
    headers = source_thread_headers(source)
    if headers is None:
        return None
    if not headers.get('decoded'):
        return thread_fields(headers.get('references'), headers.get('in_reply_to'))
    # Old Exchange captures kept threading headers only as decoded display text. Text before the first comment
    # or quote is exactly what intake decoded; after it, a decoded ")" or quote could have exposed comment text
    # as an ID, or hidden one. Where a later ID exists, the actual ancestry is unknown.
    for text in (headers['references'], headers['in_reply_to']):
        start = min((i for i in (text.find('('), text.find('"')) if i >= 0), default=-1)
        if start >= 0 and '<' in text[start:]:
            raise UndecidableThreadSyntax('Retained Exchange threading headers lost raw comment syntax; read the '
                                          'original again before a new reply. Existing accepted mail stays held')
    return {'references': _message_ids(headers['references']), 'in_reply_to': headers['in_reply_to']}


def source_thread_headers(source):
    if isinstance(source.get('thread_headers'), dict):
        return source['thread_headers']
    artifact = source.get('source_artifact') or {}
    if artifact.get('path'):
        try:
            raw = Path(artifact['path']).read_bytes()
        except OSError as exc:
            raise RetainedSourceError('Retained source is unavailable; no reply was changed or sent') from exc
        if hashlib.sha256(raw).hexdigest() != artifact.get('sha256'):
            raise RetainedSourceError('Retained source digest differs; no reply was changed or sent')
        headers = {}
        for key, value in BytesHeaderParser(policy=default).parsebytes(raw).raw_items():
            headers.setdefault(key.lower(), value)  # same first-header semantics as parse_message
        return {'references': headers.get('references', ''), 'in_reply_to': headers.get('in-reply-to', '')}
    # Old EWS captures retained threading headers only as display text, already decoded (including encoded words
    # inside comments). source_thread_fields must not decode it again or treat it as raw syntax.
    headers = source.get('headers') or {}
    if 'references' in headers or 'in-reply-to' in headers:
        return {'references': ' '.join(headers.get('references') or []),
                'in_reply_to': next(iter(headers.get('in-reply-to') or []), ''), 'decoded': True}
    return None


def legacy_ews_source(source):
    return (source.get('source_format') == 'ews_properties'
            or ('headers' in source and str(source.get('message_ref') or '').startswith('ews:'))) and 'address_names' not in source


def reply_source(source):
    """Decode an old structured capture only when its original address syntax survived.

    Pre-fix EWS rows kept only decoded display text; an apparent comma may have belonged to an encoded name.
    Such a row cannot choose recipients. A new read can supply syntax for a new request, never rewrite a queue.
    """
    if not legacy_ews_source(source):
        return source
    raw = source.get('raw_address_headers')
    if not isinstance(raw, dict):
        raise RetainedSourceError('Legacy Exchange reply source lost raw address syntax; read the original again '
                                  'before a new reply. Existing accepted mail stays held and unchanged')
    groups = {field: [pair for value in raw.get(field, []) for pair in header_mailboxes(field, value)]
              for field in ('from', 'to', 'cc', 'reply-to')}
    # A missing raw field cannot reconstruct a native EWS fallback projection from an old record.
    if any(source.get(key) and not groups[field] for field, key in
           (('from', 'sender'), ('to', 'recipients'), ('cc', 'cc'), ('reply-to', 'reply_to'))):
        raise RetainedSourceError('Legacy Exchange reply source has incomplete raw address syntax; read it again')
    return {**source, 'sender': groups['from'][0][1] if groups['from'] else '',
            'sender_name': groups['from'][0][0] if groups['from'] else '',
            'recipients': [v for _, v in groups['to']], 'cc': [v for _, v in groups['cc']],
            'reply_to': [v for _, v in groups['reply-to']],
            'address_names': address_names_of(lambda field: raw.get(field.lower(), []))}


def automatic_reply_envelope(item):
    """``(source, recipients)`` of the automatic reply to one captured mail: Reply-To, else From.

    The one definition shared by the Presence event's ``reply_targets`` and the queued reply, so the Host checks
    exactly the addresses the reply is delivered to. Raises MailAddressError when no mailbox can be derived."""
    source = reply_source({**item.context, 'message_id': item.message_id, 'sender': item.sender,
                           'references': item.references, 'in_reply_to': item.in_reply_to,
                           'recipients': item.recipients, 'cc': item.cc})
    recipients = mailbox_addresses(source.get("reply_to") or [source['sender']])
    if not recipients:
        raise MailAddressError("Selected reply has no mailbox recipient")
    return source, recipients


def automatic_reply_bare(item):
    """The bare envelope addresses (as delivered) of the automatic reply to ``item``."""
    return envelope_groups(automatic_reply_envelope(item)[1])[0]
