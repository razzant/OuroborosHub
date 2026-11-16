"""CFWS tokens cannot redirect intake, mutation, or replies; retained identities stay independent."""
import asyncio
from contextlib import nullcontext
from dataclasses import replace

import pytest

from lib.mime import _message_ids, conversation_root, emitted_conversation, parse_message, reply_chain
from lib.runtime import EmailRuntime
from lib.service import MailService
from lib.store import EmailStore
from test_binding_core import DeliveryClient, release_hold
from test_reply_identity_compatibility import configure_host, invoke
from test_reply_source_authority import service_for
from test_write_targets import host_guard

pytest_plugins = ['test_binding_core']
ROOT, DECOY, MID = '<paused-root@example.invalid>', '<decoy@example.invalid>', '<message@example.invalid>'


def raw_message():
    return (f'From: peer@example.invalid\r\nTo: owner@example.invalid\r\nMessage-ID: {MID}\r\n'
            f'References: (cfws {DECOY}) {ROOT}\r\nSubject: Source\r\n\r\nBody').encode()


@pytest.mark.parametrize(('header', 'expected'), [
    (f'(cfws {DECOY}) {ROOT}', [ROOT]), (f'{ROOT} (tail {DECOY})', [ROOT]),
    (f'(nested ({DECOY}) \\) escaped) {ROOT}', [ROOT]),
    (f'(version 1 < 2) {ROOT} {MID}', [ROOT, MID]),
    (f'(unclosed {DECOY} {ROOT}', []),
    (f'"phrase {DECOY}" {ROOT}', [ROOT]),
    ('<тест@почта.рф> <local-token> <"old local"@example.invalid>',
     ['<тест@почта.рф>', '<local-token>', '<"old local"@example.invalid>']),
])
def test_grammar_tokens(header, expected):
    assert _message_ids(header) == expected


def test_incoming_and_full_emitted_chain_use_only_actual_ids():
    message = parse_message(raw_message(), folder='INBOX', uid=1)
    assert message['references'] == [ROOT]
    assert conversation_root(references=message['references'], message_id=MID) == ROOT
    assert emitted_conversation(MID, [f'(cfws {DECOY}) {ROOT}']) == ROOT


def old_capture():
    message = parse_message(raw_message(), folder='INBOX', uid=1, include_attachment_data=True)
    message.pop('thread_headers', None)
    message.update(uidvalidity=1, references=[DECOY, ROOT])  # the old parser's projection
    return message


@pytest.mark.parametrize(('old_state', 'actual_state'), [('paused', 'active'), ('active', 'paused'), ('active', 'active')])
def test_retained_reply_checks_historical_and_actual_scopes(tmp_path, monkeypatch, core_host, old_state, actual_state):
    account, call, store, _ = service_for(tmp_path, 'imap', captured=[old_capture()])
    drive, adapter = configure_host(monkeypatch, core_host, account, [(DECOY, old_state), (ROOT, actual_state)])
    guard = host_guard(tmp_path, monkeypatch, 'email_send', account, [], drive=drive)
    args, result = invoke(call, 'email_send', dict(account_id=account, to='peer@example.invalid', body='Answer',
                        reply_to_message_id=MID, request_id='new-reply'), guard)
    assert store.inbox_event('INBOX', 1, 1).thread_key == DECOY
    if old_state == 'paused':
        assert 'Paused' in result['refusal'] and store.queued_message('new-reply') is None
        return
    assert args['conversation_id'] == DECOY and args['emitted_conversation_id'] == ROOT
    queued = store.queued_message('new-reply')
    if actual_state == 'paused':
        # The emitted group's own thread is checked at dispatch, before anything is queued.
        assert 'Paused' in result['refusal'] and queued is None
        assert store.inbox_event('INBOX', 1, 1).references == (DECOY, ROOT)
        return
    assert queued.references == (ROOT,)
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, adapter, account=account, account_loader=lambda: {'sending': 'on'})
    release_hold(store)
    assert runtime.process_outbox()
    assert bool(delivery.sent) == (actual_state == 'active')
    assert store.inbox_event('INBOX', 1, 1).references == (DECOY, ROOT)


def test_accepted_bad_chain_is_held_without_rewriting(tmp_path):
    store = EmailStore(tmp_path)
    store.ingest(old_capture())
    store.enqueue_outbox(request_id='accepted', recipients=['peer@example.invalid'], subject='s', body='b',
                         in_reply_to=MID, references=[DECOY, ROOT])
    before = store.queued_message('accepted')
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, object(), account='a', account_loader=lambda: {'sending': 'on'})
    assert runtime.process_outbox() and not delivery.sent
    after = store.queued_message('accepted')
    assert (after.request_id, after.message_id, after.references, after.recipients, after.body) == (
        before.request_id, before.message_id, before.references, before.recipients, before.body)
    assert store.receipt('accepted')['state'] == 'held' and store.receipt('accepted')['attempts'] == 0


def test_old_pending_intake_does_not_submit_into_paused_actual_ancestry(tmp_path, monkeypatch, core_host):
    account, _, store, _ = service_for(tmp_path, 'imap', captured=[old_capture()])
    _, adapter = configure_host(monkeypatch, core_host, account, [(DECOY, 'active'), (ROOT, 'paused')])
    async def discover():
        adapter.available = True
    async def forbidden(*_):
        pytest.fail('old decoy scope submitted despite paused actual ancestry')
    runtime = EmailRuntime(store, object(), adapter, account=account)
    monkeypatch.setattr(runtime, 'discover_support', discover)
    monkeypatch.setattr(adapter, 'submit', forbidden)
    assert asyncio.run(runtime.process_inbox())
    assert store.inbox_event('INBOX', 1, 1).thread_key == DECOY


def test_mutation_keeps_historical_hold_after_fresh_read(tmp_path, monkeypatch, core_host):
    account, _, store, _ = service_for(tmp_path, 'imap', captured=[old_capture()])
    _, adapter = configure_host(monkeypatch, core_host, account, [(DECOY, 'paused'), (ROOT, 'active')])
    service = MailService.__new__(MailService)
    service._presence_host = lambda: nullcontext(adapter)
    selection = {'folder': 'INBOX', 'uidvalidity': 1, 'uid': 1}
    held = service._historical_mutation_hold({'id': account}, store, selection)
    assert held['code'] == 'historical_source_held'
    # Same physical read keeps the old cached source as well as an unchanged inbox key.
    old = old_capture(); old.pop('_raw_source'); old['headers'] = {'references': [f'(cfws {DECOY}) {ROOT}']}
    store.remember_read(old)
    new = parse_message(raw_message(), folder='INBOX', uid=1); new['uidvalidity'] = 1
    store.remember_read(new)
    assert DECOY in store.historical_message_roots(selection)


def test_explicit_folder_reread_keeps_historical_reply_scope(tmp_path, monkeypatch, core_host):
    fresh = parse_message(raw_message(), folder='INBOX', uid=1)
    fresh['uidvalidity'] = 1
    account, call, store, _ = service_for(tmp_path, 'imap', captured=[old_capture()], known=[fresh])
    store.remember_read(fresh)  # forces an ambiguity between the old and fresh projections
    drive, _ = configure_host(monkeypatch, core_host, account, [(DECOY, 'paused'), (ROOT, 'active')])
    guard = host_guard(tmp_path, monkeypatch, 'email_send', account, [], drive=drive)
    args, result = invoke(call, 'email_send', dict(account_id=account, to='peer@example.invalid', body='Answer',
                         reply_to_message_id=MID, reply_folder='INBOX', request_id='reread'), guard)
    assert args['conversation_id'] == DECOY and args['emitted_conversation_id'] == ROOT
    assert 'Paused' in result['refusal'] and store.queued_message('reread') is None


@pytest.mark.parametrize('action', ['flags', 'delete_draft', 'replace_draft'])
def test_gmail_mutations_check_old_and_actual_sources(tmp_path, monkeypatch, core_host, action):
    from types import SimpleNamespace
    from test_write_targets import gmail_for
    monkeypatch.setattr("test_write_targets.tools_for", lambda path, provider, client: (client,))
    account = {'id': 'gmail:fixture', 'address': 'owner@example.invalid', 'provider': 'gmail',
               'senders': ['owner@example.invalid']}
    client, writes = gmail_for(tmp_path, drafts={'draft-one': (['DRAFT'], {'Message-ID': MID,
                                    'References': f'(cfws {DECOY}) {ROOT}'})})
    client._owns_http = False  # the fixture supplies transport methods directly
    store = EmailStore(tmp_path / 'email')
    source = old_capture()
    source.update(message_ref='gmail:d1', provider_id='d1', provider_event_key='gmail:d1')
    store.ingest(source)
    _, adapter = configure_host(monkeypatch, core_host, account['id'], [(DECOY, 'paused'), (ROOT, 'active')])
    accounts = SimpleNamespace(resolve=lambda _: account, mail_dir=lambda _: tmp_path / 'email')
    monkeypatch.setattr(MailService, 'accounts', property(lambda _: accounts))
    monkeypatch.setattr('lib.service.account_client', lambda *a, **kw: client)
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    service._presence_host = lambda: nullcontext(adapter)
    if action == 'flags':
        result = service.mailbox(account_id=account['id'], message_ref=service.wrap_ref(account, source),
                                 action='flags', add_flags=['\\Seen'])
        assert result['code'] == 'historical_source_held'
    else:
        with pytest.raises(ValueError, match='[Pp]aused'):
            if action == 'delete_draft':
                service.mailbox(account_id=account['id'], action='delete_draft', draft_id='draft-one')
            else:
                service.draft(account_id=account['id'], to='peer@example.invalid', body='b', draft_id='draft-one')
    assert not writes


@pytest.mark.parametrize('state', ['paused', 'active'])
def test_fresh_gmail_flags_targets_actual_reference_root(tmp_path, monkeypatch, state):
    from test_write_targets import gmail_for
    headers = {'Message-ID': MID, 'References': f'(cfws {DECOY}) {ROOT}'}
    account, call, writes = gmail_for(tmp_path, message_headers=headers)
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account, [('', 'active'), (ROOT, state), (DECOY, 'active')])
    args = dict(account_id=account, action='flags', uid=123, uidvalidity=1, flags=['\\Seen'])
    for _ in range(3):
        refusal = guard(args)
        if refusal:
            assert state == 'paused' and 'Paused' in refusal and not writes
            return
        result = call('email_mailbox', args)
        if result.get('status') == 'needs_arguments':
            args = result['retry_arguments']
            assert args['message_conversation_id'] == ROOT
        else:
            assert state == 'active' and len(writes) == 1
            assert writes[0] == ('POST', '/messages/7b/modify', {'addLabelIds': [], 'removeLabelIds': ['UNREAD']})
            return
    pytest.fail('canonical mutation did not settle')


@pytest.mark.parametrize('folder', [None, 'inbox', 'INBOX'])
def test_imap_cached_historical_scope_survives_default_and_casefolded_folder(tmp_path, monkeypatch, core_host, folder):
    account, call, store, client = service_for(tmp_path, 'imap')
    message = old_capture()
    store.stage_inbound_attachments(message)  # the real email_read stages raw MIME before caching
    store.remember_read(message)  # only an explicit read cache, no inbox capture
    _, adapter = configure_host(monkeypatch, core_host, account, [(DECOY, 'paused'), (ROOT, 'active')])
    monkeypatch.setattr(MailService, '_presence_host', lambda _: nullcontext(adapter))
    def no_write(**kwargs):
        pytest.fail('provider reached despite retained historical scope')
    client.mailbox = no_write
    args = dict(account_id=account, action='flags', uid=1, uidvalidity=1, flags=['\\Seen'])
    if folder is not None:
        args['folder'] = folder
    held = call('email_mailbox', args)
    assert held['code'] == 'historical_source_held' and 'paused' in held['error']
