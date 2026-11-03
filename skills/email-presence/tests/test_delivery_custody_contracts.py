"""Recipient meaning and selected speech survive the ordinary durable mail path."""
import asyncio
import base64
import os
import stat
from contextlib import contextmanager
from email import message_from_bytes
from email.policy import default
from types import SimpleNamespace

import httpx
import pytest

from lib.accounts import AccountStore
from lib.client import MailClient
from lib.gmail import GmailClient
from lib.host_adapter import LoopbackPresenceHostAdapter, _ref
from lib.mime import mailbox_addresses
from lib.runtime import EmailRuntime
from lib.service import MailService
from lib.store import EmailStore


def service_and_store(tmp_path):
    accounts = AccountStore(tmp_path)
    account = accounts.save_imap({'address': 'owner@example.invalid', 'password': 'fixture',
                                 'imap_host': 'imap.invalid', 'smtp_host': 'smtp.invalid'})
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    return account, service, EmailStore(accounts.mail_dir(account))


def decoded_raw(payload):
    encoded = payload['raw']
    return message_from_bytes(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)), policy=default)


@pytest.mark.parametrize('to', ['john@example.invalid', '"Doe, John" <john@example.invalid>'])
def test_named_recipients_keep_groups_and_bcc_on_both_transports(tmp_path, to):
    account, service, store = service_and_store(tmp_path)
    result = service.send(account_id=account['id'], to=to, cc=['"Lee, Sue" <sue@example.invalid>'],
                          bcc=['blind@example.invalid', 'john@example.invalid'], body='Message', request_id='named')
    assert result['state'] == 'pending'
    item = store.claim_outbox()
    assert set(item.envelope_recipients) == {'john@example.invalid', 'sue@example.invalid', 'blind@example.invalid'}
    assert item.bcc == ('blind@example.invalid',)
    client = MailClient({'EMAIL_USER': account['address']})
    message = client.message(item)
    calls = []
    smtp = SimpleNamespace(send_message=lambda msg, **kw: calls.append(kw['to_addrs']) or {})
    client.transmit(smtp, message, item.envelope_recipients, has_bcc=True)
    assert calls == [item.envelope_recipients]
    assert message['Bcc'] is None
    gmail = object.__new__(GmailClient)
    gmail._delivery_access = 'fixture'
    payloads = []
    gmail._request = lambda *_, **kw: payloads.append(kw['body']) or {'id': 'accepted'}
    gmail.transmit(None, message, item.envelope_recipients, has_bcc=True)
    wire = decoded_raw(payloads[0])
    assert wire['To'].addresses[0].addr_spec == 'john@example.invalid'
    assert wire['Cc'].addresses[0].display_name == 'Lee, Sue'
    assert wire['Bcc'].addresses[0].addr_spec == 'blind@example.invalid'


@pytest.mark.parametrize('to', ['', '"unterminated', 'not-an-address'])
def test_invalid_or_empty_mailbox_is_refused_before_queue(tmp_path, to):
    account, service, store = service_and_store(tmp_path)
    with pytest.raises(ValueError):
        service.send(account_id=account['id'], to=to, body='Must not send', request_id='invalid')
    assert store.receipt('invalid') is None
    with pytest.raises(ValueError):
        store.enqueue_outbox(request_id='empty', to=[], body='No envelope')
    assert store.receipt('empty') is None


def test_complete_mailbox_parser_preserves_unicode_and_quoted_local_parts(tmp_path):
    values = mailbox_addresses('"Имя, Фамилия" <имя@пример.рф>, "space local"@example.invalid')
    store = EmailStore(tmp_path)
    store.enqueue_outbox(request_id='unicode', to=values, body='Unicode envelope')
    item = store.claim_outbox()
    assert item.envelope_recipients == ['имя@пример.рф', '"space local"@example.invalid']
    message = MailClient({'EMAIL_USER': 'owner@example.invalid'}).message(item)
    assert [address.addr_spec for address in message['To'].addresses] == ['имя@пример.рф', '"space local"@example.invalid']


@pytest.mark.parametrize('provider', ['imap', 'gmail'])
def test_draft_parses_complete_quoted_address_lists(provider):
    captured = []
    if provider == 'imap':
        client = MailClient({'EMAIL_USER': 'owner@example.invalid'})
        @contextmanager
        def imap(*args, **kwargs):
            yield SimpleNamespace(append=lambda *args: (captured.append(args[-1]) or ('OK', [b'APPENDUID 1 1'])))
        client.imap = imap
    else:
        client = object.__new__(GmailClient)
        client.settings = {'EMAIL_USER': 'owner@example.invalid'}
        client._request = lambda *_, **kw: captured.append(decoded_raw(kw['body']['message']).as_bytes()) or {'id': 'draft', 'message': {'id': 'message'}}
    result = client.draft(to='"Doe, John" <john@example.invalid>', subject='Draft', bcc=['"Blind, Guest" <blind@example.invalid>'])
    assert result['ok']
    wire = message_from_bytes(captured[0], policy=default)
    assert wire['To'].addresses[0].display_name == 'Doe, John'
    assert wire['Bcc'].addresses[0].addr_spec == 'blind@example.invalid'


@pytest.mark.parametrize('terminal', ['completed', 'failed'])
@pytest.mark.parametrize('legacy_queued', [False, True])
def test_restart_keeps_selected_deferred_speech_and_releases_only_after_settlement(tmp_path, terminal, legacy_queued):
    store = EmailStore(tmp_path)
    store.ingest({'uid': 1, 'message_id': '<incoming>', 'sender': 'reader@example.invalid', 'subject': 'Question', 'body': 'Question'})
    item = store.claim_inbox()
    reference = _ref('deferred', {'work_ref': 'work-one', 'turn_ref': 'turn-one', 'text': '', 'delivery_reporting_version': 1})
    store.set_host_reference(item.row_id, item.lease_token, reference)
    store.retry_inbox(item.row_id, item.lease_token, '', 0)
    async def exercise():
        polls = []
        def handle(request):
            if request.url.path == '/identity':
                return httpx.Response(200, json={'presence_delivery_version': 1, 'presence_continuation_versions': [2]})
            assert request.url.path == '/presence/work/work-one'
            polls.append(True)
            return httpx.Response(200, json={'status': terminal, 'outcome': 'message', 'text': 'Selected answer',
                    'output_ref': 'selected-one', 'work_ref': 'work-one', 'error': 'Later failure' if terminal == 'failed' else ''})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            host = LoopbackPresenceHostAdapter('a'*32, host_service_url='http://127.0.0.1:8767', skill_token='fixture', http_client=http)
            captures = []
            def capture(*args, **kwargs):
                captures.append(True)
                if len(captures) == 1 and not legacy_queued:
                    raise httpx.ReadTimeout('temporary binding failure')
                return {'supported': False}
            host.capture_binding = capture
            runtime = EmailRuntime(EmailStore(tmp_path), MailClient({'EMAIL_USER': 'owner@example.invalid'}), host, account='mailbox')
            if legacy_queued:
                runtime._reply(item, ('Selected answer',), 'final', 1, 'turn-one')
            await runtime.process_inbox('observe')
            if not legacy_queued:
                assert 'work-one' in host._terminal  # retry still owns the exact paid output
                assert runtime.store.status()['inbox'] == {'retry': 1}
                with runtime.store._connect() as db: db.execute('UPDATE inbox SET available_at=0')
                await runtime.process_inbox('observe')
            assert host._terminal == {}
            assert len(polls) == 1
            assert runtime.store.status()['inbox'] == {terminal: 1}
            with runtime.store._connect() as db:
                outgoing = [dict(row) for row in db.execute('SELECT request_id,body FROM outbox')]
            assert len(outgoing) == 1 and outgoing[0]['body'] == 'Selected answer'
            assert outgoing[0]['request_id'].endswith(':final' if legacy_queued else ':output:selected-one')
            assert not await runtime.process_inbox('observe')
    asyncio.run(exercise())


def test_terminal_cache_keeps_a_reference_needed_by_other_unsettled_mail():
    host = LoopbackPresenceHostAdapter('', host_service_url='http://127.0.0.1:8767', skill_token='fixture', sync_only=True)
    first = _ref('retained', {'response': {'turn_ref': 'first'}, 'work_refs': ['shared']})
    second = _ref('deferred', {'work_ref': 'shared'})
    host._terminal.update(first={'text': 'First'}, shared={'text': 'Shared'})
    host.release(first, [second])
    assert host._terminal == {'shared': {'text': 'Shared'}}
    host.release(second)
    assert host._terminal == {}


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode bits do not describe Windows ACLs')
def test_new_mail_state_is_private_without_changing_existing_owner_modes(tmp_path):
    previous = os.umask(0o022)
    try: store = EmailStore(tmp_path / 'new-mail')
    finally: os.umask(previous)
    assert stat.S_IMODE(store.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    os.chmod(store.path, 0o640)
    store.put('kept', {'message': 'history'})
    reopened = EmailStore(store.state_dir)
    assert stat.S_IMODE(reopened.path.stat().st_mode) == 0o640
    assert reopened.get('kept') == {'message': 'history'}
