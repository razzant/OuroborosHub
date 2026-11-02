"""Readable header text crosses SQLite, provider tools and the real Host child writer."""
import base64
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from lib import service as service_module
from lib.accounts import AccountStore
from lib.gmail import GmailClient
from lib.mime import decode_header_value
from lib.runtime import EmailRuntime
from lib.service import MailService, register_tools
from lib.store import EmailStore
from test_accounts import gmail_account
from test_transport import FakeClient, FakeIMAP


UTF8 = 'Иван Петров'.encode('utf-8')
BAD_WORD = b'=?utf-8?b?QUJDR?='
FROM = b'From: reader@example.invalid'
TO = b'To: owner@example.invalid'
SHAPES = {
    'utf8_from': [b'From: ' + UTF8 + b' <reader@example.invalid>', TO],
    'latin1_from': [b'From: Jos\xe9 <reader@example.invalid>', TO],
    'utf8_to': [FROM, b'To: ' + UTF8 + b' <owner@example.invalid>'],
    'utf8_cc': [FROM, TO, b'Cc: ' + UTF8 + b' <cc@example.invalid>'],
    'utf8_replyto': [FROM, TO, b'Reply-To: ' + UTF8 + b' <reply@example.invalid>'],
    'utf8_list': [FROM, TO, b'List-Id: ' + UTF8 + b' <list.example.invalid>'],
    'word_from': [b'From: ' + BAD_WORD + b' <reader@example.invalid>', TO],
    'word_to': [FROM, b'To: ' + BAD_WORD + b' <owner@example.invalid>'],
    'valid_word': [b'From: =?UTF-8?B?0J/RgNC40LLQtdGC?= <reader@example.invalid>', TO],
    'folded': [FROM, b'To: Alpha <a@example.invalid>,\r\n Beta <b@example.invalid>'],
    'plain': [FROM, TO],
}


def mail(lines, native):
    headers = [*lines, b'Subject: Message', b'Message-ID: <' + native.encode() + b'@example.invalid>']
    return b'\r\n'.join([*headers, b'', b'Body stays readable', b'']), headers


@contextmanager
def mailbox(tmp_path, provider, shape):
    first_raw, first_headers = mail(SHAPES[shape], 'a1')
    second_raw, second_headers = mail([FROM, TO], 'a2')
    accounts = AccountStore(tmp_path / 'accounts')
    if provider == 'imap':
        account = accounts.save_imap({'address': 'owner@example.invalid', 'password': 'fixture',
                                     'imap_host': 'imap.invalid', 'smtp_host': 'smtp.invalid'})
        store = EmailStore(accounts.mail_dir(account))
        box = FakeIMAP(time.time())
        client = FakeClient(box)
        runtime = EmailRuntime(store, client, SimpleNamespace(), account=account['id'])
        assert runtime.poll() == 0
        box.messages.update({1: (first_raw, box.now), 2: (second_raw, box.now)})
        box.next_uid = 3
        yield SimpleNamespace(runtime=runtime, store=store, client=client, accounts=accounts, account=account,
            raw=first_raw, first=('INBOX', box.validity, 1), cursor_key=store.cursor_key(account['id'], 'INBOX'),
            cursor_before=0, cursor_after=2, cursor_field='uid')
        return
    account = gmail_account(accounts)
    store = EmailStore(accounts.mail_dir(account))
    messages = {}
    for native, raw, headers in [('a1', first_raw, first_headers), ('a2', second_raw, second_headers)]:
        messages[native] = {'id': native, 'threadId': 'b1', 'labelIds': ['INBOX'], 'internalDate': '4102444800000',
            'raw': base64.urlsafe_b64encode(raw).decode().rstrip('='),
            'payload': {'headers': [{'name': line.partition(b':')[0].decode(),
                                    'value': line.partition(b':')[2].lstrip().decode('utf-8', 'replace')} for line in headers]}}
    def handle(request):
        assert request.method == 'GET'
        if request.url.path.endswith('/profile'):
            return httpx.Response(200, json={'historyId': '100'})
        if request.url.path.endswith('/history'):
            events = [{'messagesAdded': [{'message': {'id': native}} for native in messages]}] if request.url.params['startHistoryId'] == '100' else []
            return httpx.Response(200, json={'historyId': '101', 'history': events})
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={'messages': [{'id': native} for native in messages]})
        return httpx.Response(200, json=messages[request.url.path.rsplit('/', 1)[-1]])
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = GmailClient(accounts, account['id'], http=http)
        runtime = EmailRuntime(store, client, SimpleNamespace(), account=account['id'])
        assert runtime.poll() == 0
        yield SimpleNamespace(runtime=runtime, store=store, client=client, accounts=accounts, account=account,
            raw=first_raw, first=('Gmail', 1, int('a1', 16)), cursor_key='gmail_cursor',
            cursor_before='100', cursor_after='101', cursor_field='history_id')


@pytest.mark.parametrize('source,expected', [
    ('Иван Петров <reader@example.invalid>', 'Иван Петров <reader@example.invalid>'),
    (UTF8.decode('ascii', 'surrogateescape'), 'Иван Петров'),
    (b'Jos\xe9'.decode('ascii', 'surrogateescape'), 'Jos\ufffd'),
    ('=?UTF-8?B?0J/RgNC40LLQtdGC?=', 'Привет'),
    (BAD_WORD.decode(), BAD_WORD.decode()),
    ('"Doe,\r\n John" <reader@example.invalid>', '"Doe, John" <reader@example.invalid>'),
    ('=?utf-8?b?0J/RgNC40LLQtdGC?=\r\n =?utf-8?b?0J/RgNC40LLQtdGC?=', 'ПриветПривет'),
])
def test_header_decoder_owns_unicode_folding_and_undecodable_words(source, expected):
    assert decode_header_value(source) == expected
    decode_header_value(source).encode('utf-8')


@pytest.mark.parametrize('provider', ['imap', 'gmail'])
@pytest.mark.parametrize('shape', SHAPES)
def test_headers_cross_capture_read_search_with_the_next_valid_mail(tmp_path, provider, shape):
    with mailbox(tmp_path, provider, shape) as box:
        assert box.runtime.poll() == 2 and box.runtime.poll() == 0
        assert box.store.status()['inbox'] == {'pending': 2}
        assert box.store.get(box.cursor_key)[box.cursor_field] == box.cursor_after
        first = box.store.inbox_event(*box.first)
        assert Path(first.context['source_artifact']['path']).read_bytes() == box.raw
        assert first.body == 'Body stays readable'
        found = box.client.search()['messages']
        assert len(found) == 2 and all('body' not in row for row in found)
        read = box.client.read(uid=box.first[2], uidvalidity=box.first[1], folder=box.first[0])
        for result in (first.context, found, read):
            json.dumps(result, ensure_ascii=False).encode('utf-8')
        if shape == 'utf8_from':
            assert first.context['headers']['from'] == ['Иван Петров <reader@example.invalid>']
            assert read['headers']['from'] == first.context['headers']['from']
        elif shape == 'word_from':
            assert BAD_WORD.decode() in first.context['headers']['from'][0]
        elif shape == 'folded':
            assert first.recipients == ('a@example.invalid', 'b@example.invalid')
            assert first.context['headers']['to'] == ['Alpha <a@example.invalid>, Beta <b@example.invalid>']


@pytest.mark.parametrize('provider', ['imap', 'gmail'])
def test_capture_failure_still_stops_before_cursor_advance_and_later_mail(tmp_path, monkeypatch, provider):
    with mailbox(tmp_path, provider, 'utf8_from') as box:
        ingest = box.store.ingest
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError('Synthetic capture unavailable')
        monkeypatch.setattr(box.store, 'ingest', unavailable)
        with pytest.raises(sqlite3.OperationalError):
            box.runtime.poll()
        assert box.store.status()['inbox'] == {}
        assert box.store.get(box.cursor_key)[box.cursor_field] == box.cursor_before
        monkeypatch.setattr(box.store, 'ingest', ingest)
        assert box.runtime.poll() == 2 and box.runtime.poll() == 0
        assert box.store.status()['inbox'] == {'pending': 2}


@pytest.mark.parametrize('provider', ['imap', 'gmail'])
def test_registered_read_tools_reach_actual_core_child_result_writer(tmp_path, monkeypatch, provider):
    core = os.environ.get('OUROBOROS_SOURCE_DIR')
    if not core:
        pytest.skip('Set OUROBOROS_SOURCE_DIR for the real Host child writer')
    monkeypatch.syspath_prepend(core)
    from ouroboros.extension_process_runner import _write_child_result
    with mailbox(tmp_path, provider, 'utf8_from') as box:
        api = SimpleNamespace(get_state_dir=lambda: box.accounts.state_dir, get_settings=lambda _: {})
        if provider == 'gmail':
            monkeypatch.setattr(service_module, 'GmailClient', lambda *args: box.client)
        service = MailService(api, mail_client=lambda settings: box.client)
        tools = {}
        register_tools(SimpleNamespace(register_tool=lambda name, handler, **kwargs: tools.update({name: handler})), service)
        search_text = tools['email_search'](account_id=box.account['id'])
        found = json.loads(search_text)['messages']
        selected = next(row for row in found if row['message_id'] == '<a1@example.invalid>')
        read_text = tools['email_read'](account_id=box.account['id'], message_ref=selected['message_ref'])
        for name, result in [('search', search_text), ('read', read_text)]:
            assert 'Иван Петров' in result and 'reader@example.invalid' in result
            target = tmp_path / f'{provider}-{name}-child.json'
            _write_child_result({'result_path': str(target)}, {'ok': True, 'result': result})
            assert json.loads(target.read_text(encoding='utf-8')) == {'ok': True, 'result': result}
        read = json.loads(read_text)
        assert read['body'] == 'Body stays readable'
        assert Path(read['source_artifact']['path']).read_bytes() == box.raw
