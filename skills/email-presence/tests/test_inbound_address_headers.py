"""Malformed address headers stay readable and cannot strand later provider mail."""
import base64
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from lib.accounts import AccountStore
from lib.gmail import GmailClient
from lib.mime import parse_message
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_accounts import gmail_account
from test_foundation import ACCOUNT, registered
from test_transport import FakeClient, FakeHost, FakeIMAP


def message(native, replacements=()):
    headers = [('From', 'reader@example.invalid'), ('To', 'owner@example.invalid')]
    for name, value in replacements:
        headers = [(key, old) for key, old in headers if key != name]
        headers.append((name, value))
    headers.extend([('Subject', 'Retained message'), ('Message-ID', f'<{native}@example.invalid>')])
    raw = ('\r\n'.join(f'{name}: {value}' for name, value in headers) + '\r\n\r\nBody stays readable\r\n').encode()
    return raw, headers


@pytest.mark.parametrize('name,value', [('From', 'a@['), ('To', 'a@['), ('Cc', 'user@[10.0.0.1'),
    ('Reply-To', 'x <a@[>'), ('From', '?a _, .,'), ('From', '"Doe, John" <john@example.invalid>')])
def test_public_parser_preserves_raw_address_headers_and_body(name, value):
    raw, _ = message('one', [(name, value)])
    parsed = parse_message(raw, folder='INBOX', uid=1, include_attachment_data=True)
    assert parsed['headers'][name.lower()] == [value]
    assert parsed['message_id'] == '<one@example.invalid>'
    assert parsed['body'] == 'Body stays readable' and parsed['_raw_source'] == raw
    if value == '"Doe, John" <john@example.invalid>':
        assert (parsed['sender'], parsed['sender_name']) == ('john@example.invalid', 'Doe, John')


def test_bad_second_address_header_preserves_every_original_value():
    raw, _ = message('duplicate')
    raw = raw.replace(b'Subject:', b'To: a@[\r\nSubject:')
    parsed = parse_message(raw, folder='INBOX', uid=1)
    assert parsed['recipients'] == ['owner@example.invalid']
    assert parsed['headers']['to'] == ['owner@example.invalid', 'a@[']


def test_imap_poll_and_search_capture_bad_mail_and_the_following_valid_uid(tmp_path):
    store = EmailStore(tmp_path)
    box = FakeIMAP(time.time())
    client = FakeClient(box)
    runtime = EmailRuntime(store, client, SimpleNamespace(), account='mailbox')
    assert runtime.poll() == 0  # an empty provider baseline, not a cursor reset
    bad, _ = message('bad', [('From', 'a@[')])
    good, _ = message('good')
    box.messages.update({1: (bad, box.now), 2: (good, box.now)})
    box.next_uid = 3
    assert runtime.poll() == 2 and runtime.poll() == 0
    captured = [store.inbox_event('INBOX', box.validity, uid) for uid in (1, 2)]
    assert [item.message_id for item in captured] == ['<bad@example.invalid>', '<good@example.invalid>']
    assert captured[0].context['headers']['from'] == ['a@[']
    assert Path(captured[0].context['source_artifact']['path']).read_bytes() == bad
    assert captured[1].sender == 'reader@example.invalid'
    assert store.get(store.cursor_key('mailbox', 'INBOX'))['uid'] == 2
    result = client.search()
    assert len(result['messages']) == 2
    bad_search = next(row for row in result['messages'] if row['uid'] == 1)
    assert bad_search['headers']['from'] == ['a@['] and 'body' not in bad_search
    assert client.read(uid=1, uidvalidity=box.validity)['body'] == 'Body stays readable'
    runtime.host = FakeHost()
    import asyncio
    assert asyncio.run(runtime.process_inbox())
    assert store.status()['inbox'] == {'failed': 1, 'pending': 1}
    assert asyncio.run(runtime.process_inbox())
    assert store.status()['inbox'] == {'failed': 1, 'completed': 1}
    assert store.status()['outbox'] == {'pending': 1}


def test_gmail_history_and_metadata_search_keep_bad_mail_without_losing_followers(tmp_path):
    accounts = AccountStore(tmp_path / 'accounts')
    account = gmail_account(accounts)
    store = EmailStore(tmp_path / 'mail')
    messages = {}
    for native, replacement in [('a1', [('From', 'a@[')]), ('a2', [])]:
        raw, headers = message(native, replacement)
        messages[native] = {'id': native, 'threadId': 'b1', 'labelIds': ['INBOX'], 'internalDate': '4102444800000',
            'raw': base64.urlsafe_b64encode(raw).decode().rstrip('='),
            'payload': {'headers': [{'name': key, 'value': value} for key, value in headers]}}
    def handle(request):
        assert request.method == 'GET'
        if request.url.path.endswith('/profile'):
            return httpx.Response(200, json={'historyId': '100'})
        if request.url.path.endswith('/history'):
            history = [{'messagesAdded': [{'message': {'id': native}} for native in messages]}] if request.url.params['startHistoryId'] == '100' else []
            return httpx.Response(200, json={'history': history, 'historyId': '101'})
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={'messages': [{'id': native} for native in messages]})
        return httpx.Response(200, json=messages[request.url.path.rsplit('/', 1)[-1]])
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = GmailClient(accounts, account['id'], http=http)
        runtime = EmailRuntime(store, client, SimpleNamespace(), account=account['id'])
        assert runtime.poll() == 0
        assert runtime.poll() == 2 and runtime.poll() == 0
        assert store.get('gmail_cursor')['history_id'] == '101'
        assert store.status()['inbox'] == {'pending': 2}
        first = store.inbox_event('Gmail', 1, int('a1', 16))
        assert first.context['headers']['from'] == ['a@[']
        assert Path(first.context['source_artifact']['path']).read_bytes() == message('a1', [('From', 'a@[')])[0]
        found = client.search()['messages']
        assert len(found) == 2
        assert found[0]['headers']['from'] == ['a@['] and 'body' not in found[0]
        assert found[1]['sender'] == 'reader@example.invalid'


@pytest.mark.parametrize('value', ['a@[', 'user@[10.0.0.1', '?a _, .,'])
def test_registered_send_tool_refuses_malformed_input_without_enqueue(tmp_path, monkeypatch, value):
    tools, _ = registered(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as caught:
        tools['email_send'](account_id=ACCOUNT, to=value, body='Not sent', request_id='invalid')
    # register() loads the skill under its own package, as the Host does.
    assert type(caught.value).__name__ == 'MailAddressError'
    assert EmailStore(tmp_path).receipt('invalid') is None
