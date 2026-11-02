"""Preparation gives the existing Host guard the exact RFC conversation before effects."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
import yaml

from lib.accounts import AccountStore
from lib.service import MailService, register_tools
from lib.store import EmailStore


def context(tmp_path, source):
    accounts = AccountStore(tmp_path)
    account = accounts.save_imap({'address': 'owner@example.invalid', 'password': 'fixture',
                                 'imap_host': 'imap.invalid', 'smtp_host': 'smtp.invalid'})
    calls = []
    class Client:
        def __init__(self, settings): pass
        def find_message(self, message_id, **kwargs):
            calls.append(('read', message_id))
            return source
        def draft(self, **kwargs):
            calls.append(('draft', kwargs)); return {'ok': True}
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}), mail_client=Client)
    service.capture_binding = lambda account, root: calls.append(('binding', root)) or {'supported': False}
    return account, service, EmailStore(accounts.mail_dir(account)), calls


@pytest.mark.parametrize('references', [[], ['<root>', '<earlier>']])
@pytest.mark.parametrize('operation', ['send', 'draft'])
def test_reply_preparation_returns_exact_retry_without_effect_or_mandatory_references(tmp_path, references, operation):
    source = {'message_id': '<incoming>', 'sender': 'reader@example.invalid', 'references': references,
              'in_reply_to': '', 'subject': 'Question', 'recipients': ['owner@example.invalid']}
    account, service, store, calls = context(tmp_path, source)
    root = references[0] if references else '<incoming>'
    method = getattr(service, operation)
    result = method(account_id=account['id'], reply_to_message_id='<incoming>', reply_all=True, body='Answer', subject='Re: Question')
    assert result['status'] == 'needs_arguments'
    assert calls == [('read', '<incoming>')]
    assert store.status()['outbox'] == {}
    retry = result['retry_arguments']
    assert retry['conversation_id'] == retry['thread_id'] == root
    schemas = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **kw: schemas.update({name: kw['schema']})), service)
    import jsonschema
    jsonschema.validate(retry, schemas['email_' + operation])
    if operation == 'send': assert retry['request_id']
    answered = method(**retry)
    assert answered['ok']
    assert calls[-1][0] == ('binding' if operation == 'send' else 'draft')
    if operation == 'send':
        same = method(**retry)
        assert same['deduplicated']
        assert store.status()['outbox'] == {'pending': 1}


def test_wrong_supplied_identity_is_refused_and_new_mail_needs_no_preparation(tmp_path):
    source = {'message_id': '<incoming>', 'references': ['<root>'], 'in_reply_to': '', 'sender': 'reader@example.invalid'}
    account, service, store, calls = context(tmp_path, source)
    wrong = service.send(account_id=account['id'], to='reader@example.invalid', body='Answer', reply_to_message_id='<incoming>',
                         conversation_id='<other>', thread_id='<other>')
    assert wrong['code'] == 'conversation_identity_mismatch'
    assert store.status()['outbox'] == {} and not any(call[0] == 'binding' for call in calls)
    sent = service.send(account_id=account['id'], to='reader@example.invalid', body='New message', request_id='new')
    assert sent['ok'] and sent['state'] == 'pending'
    assert calls[-1] == ('binding', store.message_id_for('new'))


@pytest.mark.parametrize('explicit_account', [False, True])
def test_legacy_queued_reply_replays_without_source_lookup_or_new_binding(tmp_path, explicit_account):
    account, service, store, calls = context(tmp_path, None)
    store.enqueue_outbox(request_id='legacy', to=['reader@example.invalid'], body='Answer', in_reply_to='<gone>',
                         reporting={'sender': account['address']})
    account_args = {'account_id': account['id']} if explicit_account else {}
    result = service.send(**account_args, to='reader@example.invalid', body='Answer',
                          reply_to_message_id='<gone>', request_id='legacy')
    assert result['deduplicated'] and calls == []
    changed = service.send(**account_args, to='reader@example.invalid', body='Changed',
                           reply_to_message_id='<gone>', request_id='legacy')
    assert changed['code'] == 'request_id_conflict' and calls == []


@pytest.mark.parametrize('implicit_account', [False, True])
def test_prepared_reply_reaches_real_author_guard_without_reference_cartesian_product(tmp_path, monkeypatch, implicit_account):
    core = os.environ.get('OUROBOROS_SOURCE_DIR')
    if not core: pytest.skip('Set OUROBOROS_SOURCE_DIR for the real host author guard')
    monkeypatch.syspath_prepend(core)
    from ouroboros import extension_registry_state as registry, place_guard, places
    from ouroboros.extension_surface_names import extension_surface_name
    from ouroboros.conversation_lease import PresenceTurnGate
    from ouroboros.presence_bindings import conversation_key
    source = {'message_id': '<incoming>', 'references': ['<root>', '<parent>'], 'in_reply_to': '<parent>', 'sender': 'reader@example.invalid'}
    account, service, store, calls = context(tmp_path / 'mail', source)
    manifest = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath('SKILL.md').read_text(encoding='utf-8').split('---', 2)[1])
    name = extension_surface_name('email-presence', 'email_send')
    descriptor = {'skill': 'email-presence', 'extension_generation': 'mail-test'}
    bundle = registry._ExtensionRegistrations(generation_digest='mail-test', loaded_skill=SimpleNamespace(manifest=SimpleNamespace(raw_extra=manifest)))
    monkeypatch.setitem(registry._extensions, 'email-presence', bundle)
    monkeypatch.setitem(registry._tools, name, descriptor)
    drive = tmp_path / 'host'
    places.connect_place(drive, connector='email-presence', provider='email', state='active')
    key = conversation_key('email', account['id'], '<root>', '<root>')
    lease = PresenceTurnGate(state_root=drive / 'state').acquire(key)
    lease.bind_author('actual-author')
    try:
        ctx = SimpleNamespace(drive_root=drive, task_id='competitor', task_metadata={})
        args = {'to': 'reader@example.invalid', 'body': 'Answer', 'reply_to_message_id': '<incoming>'}
        args.update({'conversation_id': '<root>', 'thread_id': '<root>'} if implicit_account else {'account_id': account['id']})
        assert place_guard.dispatch_refusal(ctx, name, args, ext_tool=descriptor) == ''
        handlers = {}
        register_tools(SimpleNamespace(register_tool=lambda name, handler, **kw: handlers.update({name: handler})), service)
        prepared = json.loads(handlers['email_send'](ctx=ctx, **args))
        assert prepared['status'] == 'needs_arguments' and store.status()['outbox'] == {}
        assert not any(call[0] == 'binding' for call in calls)
        retry = prepared['retry_arguments']
        assert retry['account_id'] == account['id']
        refusal = place_guard.dispatch_refusal(ctx, name, retry, ext_tool=descriptor)
        assert 'actual-author' in refusal
        facts = place_guard.call_place_facts(ctx, name, retry, ext_tool=descriptor)
        assert facts['places'][0]['conversation_keys'] == [key]
        draft_name = extension_surface_name('email-presence', 'email_draft')
        monkeypatch.setitem(registry._tools, draft_name, descriptor)
        assert place_guard.dispatch_refusal(ctx, draft_name, retry, ext_tool=descriptor) == ''
        assert place_guard.call_place_facts(ctx, draft_name, retry, ext_tool=descriptor)['effect'] == 'write'
        ctx.task_id = 'actual-author'
        assert place_guard.dispatch_refusal(ctx, name, retry, ext_tool=descriptor) == ''
        result = service.send(**retry)
        assert result['ok']
        assert calls[-1] == ('binding', '<root>')
    finally:
        lease.release()


@pytest.mark.parametrize('operation', ['send', 'draft'])
@pytest.mark.parametrize('threaded', [False, True])
def test_omitted_sole_account_prepares_before_staging_or_effect_even_with_thread(tmp_path, monkeypatch, operation, threaded):
    source = {'message_id': '<incoming>', 'sender': 'reader@example.invalid', 'references': ['<root>']}
    account, service, store, calls = context(tmp_path, source)
    args = {'to': 'reader@example.invalid', 'body': 'Message', 'subject': 'Subject',
            'attachments': [{'filename': 'note.txt', 'data': 'Complete attachment'}]}
    if threaded:
        args.update(reply_to_message_id='<incoming>', references=['<root>'], conversation_id='<root>', thread_id='<root>')
    staged = []
    original = EmailStore.stage_outbound_attachments
    def stage(self, *args, **kwargs):
        staged.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(EmailStore, 'stage_outbound_attachments', stage)
    response = getattr(service, operation)(**args)
    assert response['status'] == 'needs_arguments' and response['code'] == 'canonical_account_required'
    assert staged == [] and calls == [] and store.status()['outbox'] == {}
    retry = response['retry_arguments']
    assert retry['account_id'] == account['id']
    for key, value in args.items():
        assert retry[key] == value
    schemas = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **kw: schemas.update({name: kw['schema']})), service)
    import jsonschema
    jsonschema.validate(retry, schemas['email_' + operation])
    result = getattr(service, operation)(**retry)
    assert result['ok'] and staged == [True]
    assert calls[-1][0] == ('binding' if operation == 'send' else 'draft')
    if operation == 'send':
        assert retry['request_id'] == result['request_id']
        if not threaded:
            assert calls[-1][1] == store.message_id_for(retry['request_id'])


def test_prepared_account_keeps_sender_alias_and_binding_outage_retry_identity(tmp_path):
    account, service, store, calls = context(tmp_path, None)
    account['senders'].append('alias@example.invalid')
    AccountStore(tmp_path).save(account)
    prepared = service.send(sender='alias@example.invalid', to='reader@example.invalid', body='Message')
    retry = prepared['retry_arguments']
    assert retry['account_id'] == account['id'] and retry['sender'] == 'alias@example.invalid'
    captured = []
    def binding(selected, root):
        captured.append((selected['id'], root))
        if len(captured) == 1:
            raise httpx.ReadTimeout('Synthetic binding outage')
        return {'supported': False}
    service.capture_binding = binding
    with pytest.raises(httpx.ReadTimeout):
        service.send(**retry)
    assert store.receipt(retry['request_id']) is None
    sent = service.send(**retry)
    assert sent['ok'] and sent['sender'] == 'alias@example.invalid'
    assert captured[0] == captured[1] == (account['id'], store.message_id_for(retry['request_id']))
    repeated = service.send(**retry)
    assert repeated['deduplicated'] and len(captured) == 2
