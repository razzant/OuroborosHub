"""UTF-8 IMAP search: exact wire for a date-bounded multiword search, and the procedure when a server misses it.

A live server was observed to find a message by an ASCII word of its subject while the same message's full Cyrillic
subject, and a single Cyrillic word of it, found nothing. The live wire was not captured; the correct CHARSET UTF-8 literal
is proven only by the synthetic fixture below. The live mismatch cause remains unknown. The second test models one possible server (matching the undecoded header, where
Cyrillic sits in RFC 2047 encoded words); it documents the supported procedure, not that server's behaviour:
page through FROM and date criteria, then filter the decoded subjects locally. All data here are synthetic.
"""
import time
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate

import pytest

import test_imap_search_pages as wire
from test_imap_search_pages import mail  # noqa: F401  (the in-process IMAP server fixture)

SUBJECT = 'Acme — Ваш временный код входа'
WORDS = 'Ваш временный код входа'
SENDER = 'noreply@billing.example.invalid'


def add(folder, subject, sender, day, message_id):
    message = EmailMessage()
    message['From'] = f'Billing <{sender}>'
    message['To'] = 'reader@example.invalid'
    message['Subject'] = subject
    message['Message-ID'] = message_id
    arrived = time.mktime(time.strptime(day, '%d-%b-%Y')) + 12 * 3600
    message['Date'] = formatdate(arrived)
    message.set_content('Synthetic body')
    raw = message.as_bytes(policy=policy.SMTP)
    assert b'=?utf-8?' in raw.split(b'\r\n\r\n')[0], 'the Cyrillic subject travels as RFC 2047 encoded words'
    return folder.add(raw, arrived)


def plant(server):
    folder = server.folders['INBOX']
    wanted = add(folder, SUBJECT, SENDER, '05-Nov-2026', '<wanted@example.invalid>')
    add(folder, 'Acme — receipt', SENDER, '20-Oct-2026', '<other@example.invalid>')
    add(folder, SUBJECT, SENDER, '20-Nov-2026', '<late@example.invalid>')  # outside the date range
    add(folder, SUBJECT, 'someone@else.example.invalid', '06-Nov-2026', '<foreign@example.invalid>')
    return wanted


def test_date_bounded_multiword_utf8_search_sends_one_exact_literal(mail):
    wanted = plant(mail.server)
    mark = len(mail.server.log)
    found = mail.service.search(account_id=mail.first, criteria=[
        'SINCE', '1-Sep-2026', 'BEFORE', '15-Nov-2026', 'FROM', SENDER, 'SUBJECT', f'"{WORDS}"'], limit=50)
    literal = WORDS.encode('utf-8')
    assert (f'UID SEARCH CHARSET UTF-8 SINCE 1-Sep-2026 BEFORE 15-Nov-2026 FROM {SENDER} '
            f'SUBJECT {{{len(literal)}}}{WORDS}') in mail.server.commands(mark)
    # This fixture server matches decoded subjects, so the exact command finds the one message in range.
    assert [message['uid'] for message in found['messages']] == [wanted]
    assert found['messages'][0]['subject'] == SUBJECT


class RawHeaderServer(wire.Server):
    """A server whose SUBJECT compares the undecoded header text (one hypothesis; not a claim about any provider)."""

    def view(self, raw, arrived):
        view = dict(super().view(raw, arrived))
        undecoded = message_from_bytes(raw, policy=policy.compat32)['Subject']  # e.g. "Acme =?utf-8?b?...?="
        view['SUBJECT'] = str(undecoded).replace('\r\n', '').casefold()
        return view


def test_when_server_subject_search_misses_page_by_sender_and_date_then_filter_decoded_subjects(mail):
    mail.server.__class__ = RawHeaderServer  # the running fixture server, now matching undecoded subjects
    mail.server.views.clear()
    wanted = plant(mail.server)
    criteria = ['SINCE', '1-Sep-2026', 'BEFORE', '15-Nov-2026', 'FROM', SENDER]
    # The whole Cyrillic phrase and a single Cyrillic word both miss; the ASCII word in the subject is found.
    for words in (f'"{WORDS}"', 'временный'):
        assert mail.service.search(account_id=mail.first, criteria=[*criteria, 'SUBJECT', words])['total'] == 0
    assert wanted in [m['uid'] for m in mail.service.search(account_id=mail.first, criteria=[*criteria, 'SUBJECT', 'Acme'])['messages']]
    # Procedure: every page of the sender/date search, then the decoded subject compared locally.
    matches, token = [], ''
    for _ in range(10):
        page = mail.service.search(account_id=mail.first, criteria=criteria, limit=2, page_token=token)
        matches += [m for m in page['messages'] if WORDS.casefold() in m['subject'].casefold()]
        token = page['next_page_token']
        if not token:
            break
    else:
        pytest.fail('paging did not end')
    assert [m['uid'] for m in matches] == [wanted] and matches[0]['message_id'] == '<wanted@example.invalid>'
