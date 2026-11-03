"""Malformed destinations remain diagnostic without ending active authors or workers."""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lib.client import MailClient
from lib.delivery import email_report
from lib.host_adapter import HostDelivery, HostOutput, HostTurnStatus, LoopbackPresenceHostAdapter, _ref
from lib.mime import MailAddressError, mailbox_addresses
from lib.runtime import EmailRuntime
from lib.store import EmailStore, OutboxItem


@pytest.mark.parametrize('value', ['a@', '"x"@', 'a@[', 'user@[10.0.0.1', 'x <a@[>',
                                  'a@b.example, c@[', '?a _, .,', 'MAILER-DAEMON', '<>', 'broken\nheader'])
def test_malformed_mailbox_has_only_the_typed_input_failure(value):
    with pytest.raises(MailAddressError) as caught:
        mailbox_addresses(value)
    assert isinstance(caught.value, ValueError)
    assert value not in str(caught.value)


@pytest.mark.parametrize('wire', [{}, {'recipients': ['recorded@example.invalid']}])
def test_delivery_report_uses_recorded_facts_without_revalidating_legacy_input(wire):
    item = OutboxItem(1, 'lease', 'legacy', ('MAILER-DAEMON',), 'Subject', 'Body', '', (), 5, '<legacy>',
                      reporting={'version': 1, 'wire': wire})
    report = email_report(item, 'failed', error='MailAddressError')
    assert report['message']['recipients'] == wire.get('recipients', ['MAILER-DAEMON'])
    assert not report['message']['smtp_accepted'] and report['text'] == ''


@pytest.mark.parametrize('version', [0, 1])
@pytest.mark.parametrize('address', ['MAILER-DAEMON', 'a@['])
def test_invalid_legacy_row_cannot_stop_later_mail_in_the_same_worker(tmp_path, version, address):
    store = EmailStore(tmp_path)
    store.enqueue_outbox(request_id='legacy', to=['reader@example.invalid'], body='Old body',
                         reporting={'version': version, 'account_id': 'mailbox'})
    with store._connect() as db:
        db.execute('UPDATE outbox SET recipients_json=?,to_json=?,attempts=4 WHERE request_id=?',
                   (json.dumps([address]), json.dumps([address]), 'legacy'))
    store.enqueue_outbox(request_id='good', to=['reader@example.invalid'], body='Later body')
    sent = []
    client = MailClient({'EMAIL_USER': 'owner@example.invalid'})
    @contextmanager
    def delivery():
        yield SimpleNamespace(send_message=lambda message, **kwargs: sent.append(message) or {})
    client.delivery = delivery
    runtime = EmailRuntime(store, client, SimpleNamespace(), account='mailbox', account_loader=lambda: {'sending': 'on'})
    assert runtime.process_outbox()
    assert store.receipt('legacy')['state'] == 'failed' and not sent
    if version:
        assert store.next_delivery_report()[2]['state'] == 'failed'
    assert runtime.process_outbox()
    assert store.receipt('good')['state'] == 'completed' and len(sent) == 1


def receive(store, uid=1, sender='MAILER-DAEMON'):
    store.ingest({'uid': uid, 'uidvalidity': 1, 'message_id': f'<incoming-{uid}>', 'sender': sender,
                  'references': ['<root>'], 'subject': 'Question', 'body': 'Captured mail'})


def persisted(store):
    with store._connect() as db:
        return dict(db.execute('SELECT state,last_error,host_reference FROM inbox WHERE uid=1').fetchone())


@pytest.mark.parametrize('sender', ['MAILER-DAEMON', 'a@[', 'reader@example.invalid'])
@pytest.mark.parametrize('reference_kind', ['completed', 'deferred'])
def test_terminal_invalid_recipient_keeps_exact_paid_selection_without_resubmission(tmp_path, sender, reference_kind):
    store = EmailStore(tmp_path)
    receive(store, sender=sender)
    item = store.claim_inbox()
    reference = (_ref('completed', {'status': 'completed', 'text': 'Retained selected answer', 'turn_ref': 'turn-one',
                                    'continuation_version': 2, 'output_ref': 'selected-one'}) if reference_kind == 'completed'
                 else _ref('deferred', {'work_ref': 'work-one', 'turn_ref': 'turn-one', 'text': ''}))
    store.set_host_reference(item.row_id, item.lease_token, reference)
    store.retry_inbox(item.row_id, item.lease_token, '', 0)
    host = LoopbackPresenceHostAdapter('', host_service_url='http://127.0.0.1:8767', skill_token='fixture', sync_only=True)
    host.available = True; host.delivery_reporting_status = 'unsupported'
    host.capture_binding = lambda *args, **kwargs: {'supported': False}
    reads = []
    async def retained_work(reference):
        reads.append(reference)
        return {'status': 'completed', 'outcome': 'message', 'text': 'Retained selected answer', 'output_ref': 'selected-one'}
    host._get_work = retained_work
    runtime = EmailRuntime(store, SimpleNamespace(settings={'EMAIL_USER': 'owner@example.invalid'}), host, account='mailbox')
    async def exercise():
        assert await runtime.process_inbox('observe')
        saved = persisted(store)
        assert saved['host_reference'] == reference
        assert (await host.status(saved['host_reference'])).outputs[0].text == 'Retained selected answer'
        if reference_kind == 'deferred':
            assert reads == ['work-one', 'work-one']  # recoverable after the terminal cache was released
        assert not await runtime.process_inbox('observe')
        if sender != 'reader@example.invalid':
            assert saved['state'] == 'failed' and 'invalid_recipient' in saved['last_error']
            assert store.status()['outbox'] == {}
        else:
            assert saved['state'] == 'completed' and store.status()['outbox'] == {'pending': 1}
    asyncio.run(exercise())


def test_bad_output_does_not_end_pending_author_or_starve_later_valid_anchor(tmp_path):
    store = EmailStore(tmp_path)
    receive(store)
    receive(store, uid=2, sender='reader@example.invalid')
    item = store.claim_inbox()
    reference = 'continuing:retained-test-reference'
    store.set_host_reference(item.row_id, item.lease_token, reference)
    store.retry_inbox(item.row_id, item.lease_token, '', 0)
    target = store.inbox_event('INBOX', 1, 2)
    anchor = {'source_event_id': target.provider_event_key,
              'message': {'folder': 'INBOX', 'uidvalidity': 1, 'imap_uid': 2, 'message_id': target.message_id}}
    bad = HostOutput('bad-output', 'First selected answer', 'author')
    good = HostOutput('good-output', 'Later valid answer', 'author', anchor)
    states = [HostTurnStatus('pending', outputs=(bad,)), HostTurnStatus('pending', outputs=(bad, good)),
              HostTurnStatus('ready', outputs=(bad, good))]
    class Host:
        available = True
        async def submit(self, item): pytest.fail('Retained author must not be resubmitted')
        async def status(self, reference): return states.pop(0)
        async def deliver(self, reference): return HostDelivery(())
    runtime = EmailRuntime(store, SimpleNamespace(settings={'EMAIL_USER': 'owner@example.invalid'}), Host(), account='mailbox')
    async def exercise():
        for expected in ('retry', 'retry', 'failed'):
            with store._connect() as db: db.execute('UPDATE inbox SET available_at=0 WHERE uid=1')
            assert await runtime.process_inbox('observe')
            saved = persisted(store)
            assert saved['state'] == expected and saved['host_reference'] == reference
            assert 'invalid_recipient' in saved['last_error']
        with store._connect() as db:
            rows = [dict(row) for row in db.execute('SELECT body,recipients_json FROM outbox')]
        assert rows == [{'body': 'Later valid answer', 'recipients_json': json.dumps(['reader@example.invalid'])}]
        assert not await runtime.process_inbox('observe')
    asyncio.run(exercise())


@pytest.mark.parametrize('cause', [ValueError('Host failure'), RuntimeError('Temporary host failure')])
def test_other_errors_do_not_become_permanent_recipient_failures(tmp_path, cause):
    store = EmailStore(tmp_path)
    receive(store, sender='reader@example.invalid')
    item = store.claim_inbox()
    store.set_host_reference(item.row_id, item.lease_token, 'existing-reference')
    store.retry_inbox(item.row_id, item.lease_token, '', 0)
    class Host:
        available = True
        async def status(self, reference): raise cause
    asyncio.run(EmailRuntime(store, SimpleNamespace(), Host()).process_inbox('observe'))
    assert persisted(store)['state'] == 'retry' and persisted(store)['host_reference'] == 'existing-reference'
