"""Provider headers survive persistence without becoming transport identity."""

import asyncio
from dataclasses import replace
from email.message import EmailMessage
import json
import sqlite3

import httpx
import pytest

from lib.client import MailClient
from lib.host_adapter import LoopbackPresenceHostAdapter, email_presence_event
from lib.mime import parse_message
from lib.runtime import EmailRuntime
from lib.store import EmailStore


def _mail(*, to='Project list <project@example.org>, Reviewer <reviewer@example.org>',
          reply_to='Reply Desk <replies@example.org>, Coordinator <coord@example.org>'):
    message = EmailMessage()
    message['From'] = 'Мира Example <mira@example.org>'
    if to is not None:
        message['To'] = to
    message['Cc'] = 'Rowan Example <rowan@example.org>, Other Reader <other@example.org>'
    if reply_to is not None:
        message['Reply-To'] = reply_to
    message['Date'] = 'Fri, 18 Sep 2026 12:30:00 +0300'
    message['Message-ID'] = '<current@example.org>'
    message['In-Reply-To'] = '<parent@example.org>'
    message['References'] = '<root@example.org> <parent@example.org>'
    message['Subject'] = 'Review the draft'
    message.set_content('The attachment is the draft; please reply to the desk.')
    message.add_attachment(b'not downloaded to the model', maintype='application',
                           subtype='pdf', filename='Черновик.pdf')
    return message.as_bytes()


@pytest.mark.parametrize('to, recipients', [
    ('Project list <project@example.org>, Reviewer <reviewer@example.org>',
     ['project@example.org', 'reviewer@example.org']),
    (None, []),  # Bcc/envelope delivery can omit the mailbox from To entirely.
])
def test_raw_mime_context_reaches_host_after_restart(tmp_path, monkeypatch, to, recipients):
    account = 'inbox@example.org'
    monkeypatch.setenv('EMAIL_USER', account)
    parsed = parse_message(_mail(to=to), folder='INBOX', uid=42)
    parsed['uidvalidity'] = 7
    assert parsed['recipients'] == recipients
    assert parsed['cc'] == ['rowan@example.org', 'other@example.org']
    assert parsed['sender_name'] == 'Мира Example'
    store = EmailStore(tmp_path)
    store.ingest(parsed)
    item = EmailStore(tmp_path).claim_inbox()

    async def exercise():
        def handler(request):
            if request.url.path == '/identity':
                return httpx.Response(200, json={'ok': True})
            event = json.loads(request.content)['event']
            assert event['source_event_id'] == 'INBOX:7:42:<current@example.org>'
            assert event['account_id'] == account
            assert event['conversation']['mailbox'] == account
            assert event['conversation_id'] == '<root@example.org>'
            assert event['actor'] == {
                'platform': 'email', 'platform_actor_id': 'mira@example.org',
                'display_name': 'Мира Example',
            }
            fact = event['message']
            assert fact['to'] == recipients
            assert fact['cc'] == parsed['cc']
            assert fact['date'] == 'Fri, 18 Sep 2026 12:30:00 +0300'
            assert fact['sent_at'] == '2026-09-18T12:30:00+03:00'  # the Date header, never a receipt time
            assert fact['reply_to'] == ['replies@example.org', 'coord@example.org']
            assert fact['headers'] == parsed['headers']
            assert 'Мира Example' in fact['headers']['from'][0]
            assert 'Other Reader' in fact['headers']['cc'][0]
            assert fact['attachments'] == [{
                'file_name': 'Черновик.pdf', 'mime_type': 'application/pdf',
                'disposition': 'attachment', 'content_available': False,
            }]
            assert 'not downloaded to the model' not in json.dumps(event)
            return httpx.Response(200, json={
                'status': 'completed', 'outcome': 'silent', 'text': '',
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter(
                'a' * 32, host_service_url='http://127.0.0.1:8767',
                skill_token='test-token', http_client=http,
            )
            await host.submit(item)

    asyncio.run(exercise())
    # A Date without a time zone ("-0000") or an unreadable one leaves the send time unknown.
    for date in ('Fri, 18 Sep 2026 12:30:00 -0000', '1 Jan 1000000000000000000 00:00:00 +0000'):
        unknown = email_presence_event(replace(item, context={**item.context, 'date': date}), account_id=account)
        assert unknown['message']['date'] == date and 'sent_at' not in unknown['message']


@pytest.mark.parametrize('reply_to, expected', [
    ('Reply Desk <replies@example.org>, Coordinator <coord@example.org>',
     ('replies@example.org', 'coord@example.org')),
    (None, ('mira@example.org',)),
])
def test_reply_target_and_thread_headers_survive_restart(tmp_path, monkeypatch, reply_to, expected):
    monkeypatch.setenv('EMAIL_USER', 'inbox@example.org')
    parsed = parse_message(_mail(reply_to=reply_to), folder='INBOX', uid=42)
    parsed['uidvalidity'] = 7
    EmailStore(tmp_path).ingest(parsed)
    store = EmailStore(tmp_path)
    client = MailClient({'EMAIL_USER': 'inbox@example.org'})

    async def exercise():
        def handler(request):
            if request.url.path == '/identity':
                return httpx.Response(200, json={'ok': True})
            assert json.loads(request.content)['event']['actor']['platform_actor_id'] == 'mira@example.org'
            return httpx.Response(200, json={
                'status': 'completed', 'outcome': 'message', 'text': 'Reviewed.',
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter(
                'a' * 32, host_service_url='http://127.0.0.1:8767',
                skill_token='test-token', http_client=http,
            )
            assert await EmailRuntime(store, client, host).process_inbox()

    asyncio.run(exercise())
    first = store.claim_outbox()
    assert first.recipients == expected
    assert first.in_reply_to == '<current@example.org>'
    assert first.references == ('<root@example.org>', '<parent@example.org>')
    store.retry_outbox(first.row_id, first.lease_token, 'Before SMTP connection', delay=0)
    resumed = EmailStore(tmp_path).claim_outbox()
    assert resumed.message_id == first.message_id
    assert resumed.recipients == expected
    rendered = client.message(resumed)
    assert str(rendered['To']) == ', '.join(expected)
    assert rendered['Cc'] is None
    assert rendered['Message-ID'] == first.message_id
    assert rendered['In-Reply-To'] == '<current@example.org>'
    assert rendered['References'] == '<root@example.org> <parent@example.org> <current@example.org>'


def test_additive_migration_keeps_old_inbox_cursor_and_receipts(tmp_path):
    store = EmailStore(tmp_path)
    cursor = store.prepare_cursor('inbox@example.org', 'INBOX', 7, 43)
    store.ingest({'uid': 42, 'uidvalidity': 7, 'message_id': '<old@example.org>',
                  'sender': 'old@example.org', 'recipients': ['alias@example.org'],
                  'body': 'Old body'})
    leased = store.claim_inbox()
    store.set_host_reference(leased.row_id, leased.lease_token, 'saved-reference')
    store.retry_inbox(leased.row_id, leased.lease_token, 'awaiting result', delay=0)
    store.enqueue_outbox(request_id='already-sent', recipients=['old@example.org'],
                         subject='Old subject', body='Old reply')
    out = store.claim_outbox()
    store.complete_outbox(out.row_id, out.lease_token)
    old_receipt = store.receipt('already-sent')
    # The published 0.1.0 schema differs by this one missing metadata column.
    with sqlite3.connect(store.path) as db:
        db.execute('ALTER TABLE inbox DROP COLUMN context_json')
    migrated = EmailStore(tmp_path)
    old = migrated.claim_inbox()
    assert old.context == {}
    assert old.body == 'Old body'
    assert old.host_reference == 'saved-reference'
    assert old.provider_event_key == 'INBOX:7:42:<old@example.org>'
    assert migrated.get(migrated.cursor_key('inbox@example.org', 'INBOX')) == cursor
    assert migrated.receipt('already-sent') == old_receipt
    event = email_presence_event(old, account_id='inbox@example.org')
    assert event['conversation']['mailbox'] == 'inbox@example.org'
    assert event['message']['to'] == ['alias@example.org']
    assert 'date' not in event['message']
    assert migrated.claim_outbox() is None
    migrated.complete_inbox(old.row_id, old.lease_token)
    migrated.ingest(parse_message(_mail(), folder='INBOX', uid=43))
    fresh = EmailStore(tmp_path).claim_inbox()
    assert fresh.context['sender_name'] == 'Мира Example'
    assert fresh.context['reply_to'] == ['replies@example.org', 'coord@example.org']
