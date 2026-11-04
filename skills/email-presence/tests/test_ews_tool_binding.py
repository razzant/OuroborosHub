"""Public account references and RFC reply custody across the EWS tool boundary."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from lib.accounts import AccountError, AccountStore
from lib.service import MailService, register_tools
from lib.store import EmailStore


@pytest.fixture
def exchange_tools(tmp_path, monkeypatch):
    from lib import ews
    registry = AccountStore(tmp_path)
    def add(address):
        return registry.save_ews({'address': address, 'endpoint': 'https://mail.example.invalid/EWS/Exchange.asmx',
                                  'username': 'delegated-login', 'password': 'fixture-only'})
    first, second = add('work@example.invalid'), add('other@example.invalid')
    calls = []
    def message(reference):
        return {'message_ref': reference, 'message_id': '<source@example.invalid>',
                'references': ['<root@example.invalid>'], 'provider_thread_id': 'exchange-conversation',
                'sender': 'reader@example.invalid', 'recipients': ['work@example.invalid'], 'body': 'Hello'}
    class Provider:
        def __init__(self, account): self.account = account['id']
        def search(self, **args):
            calls.append(('search', self.account, args))
            return {'messages': [message('ews:AA/Case+Sensitive==')], 'next_page_token': '7'}
        def read(self, *, message_ref, include_attachment_data):
            calls.append(('read', self.account, message_ref))
            return message(message_ref)
        def mailbox(self, **args):
            calls.append(('mailbox', self.account, args))
            return {'ok': True, 'message_ref': 'ews:new/Location+ID=='}
        def draft(self, **args):
            calls.append(('draft', self.account, args))
            return {'ok': True, 'message_ref': 'ews:new/Draft+ID=='}
    monkeypatch.setattr(ews, 'EwsClient', Provider)
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    handlers = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **_: handlers.update({name: handler})), service)
    def call(name, **args):
        return json.loads(handlers[name](**args))
    return first, second, call, calls, EmailStore(registry.mail_dir(first))


def test_opaque_refs_remain_bound_across_search_read_and_move(exchange_tools):
    first, second, call, calls, _ = exchange_tools
    page = call('email_search', account_id=first['id'], query='project', page_token='5', limit=2)
    assert page['next_page_token'] == '7' and calls[-1][2]['page_token'] == '5'
    ref = page['messages'][0]['message_ref']
    assert ref.startswith('mail:')
    with pytest.raises(AccountError, match='another account'):
        call('email_read', account_id=second['id'], message_ref=ref)
    assert not any(c[0] == 'read' for c in calls)
    read = call('email_read', account_id=first['id'], message_ref=ref)
    assert calls[-1] == ('read', first['id'], 'ews:AA/Case+Sensitive==')
    assert read['thread_id'] == '<root@example.invalid>'
    moved = call('email_mailbox', account_id=first['id'], action='move', message_ref=ref, destination='Archive')
    assert calls[-1][2] == {'action': 'move', 'message_ref': 'ews:AA/Case+Sensitive==', 'destination': 'Archive'}
    assert moved['message_ref'].startswith('mail:') and moved['message_ref'] != ref
    call('email_read', account_id=first['id'], message_ref=moved['message_ref'])
    assert calls[-1] == ('read', first['id'], 'ews:new/Location+ID==')


def test_exchange_native_thread_is_never_the_rfc_author_or_a_gmail_route(exchange_tools):
    first, _, call, calls, store = exchange_tools
    ref = call('email_search', account_id=first['id'])['messages'][0]['message_ref']
    read = call('email_read', account_id=first['id'], message_ref=ref)
    args = {'account_id': first['id'], 'reply_to_message_id': read['message_id'],
            'reply_all': True, 'to': '', 'body': 'Reply', 'subject': 'Re: Question'}
    prepared = call('email_draft', **args)
    assert prepared['status'] == 'needs_arguments' and not any(c[0] == 'draft' for c in calls)
    retry = prepared['retry_arguments']
    assert retry['thread_id'] == retry['conversation_id'] == '<root@example.invalid>'
    draft = call('email_draft', **retry)
    assert draft['message_ref'].startswith('mail:') and 'thread_id' not in calls[-1][2]
    sent = call('email_send', **args, conversation_id=read['conversation_id'], thread_id=read['thread_id'],
                request_id='fixture-reply')
    assert sent['ok'] and store.status()['outbox'] == {'pending': 1}
    queued = store.queued_message('fixture-reply')
    assert queued.in_reply_to == read['message_id'] and 'gmail_thread_id' not in queued.reporting


def test_companion_drains_known_pages_and_returns_to_normal_idle_wait(tmp_path):
    from lib.runtime import EmailRuntime
    from test_transport import FakeHost

    async def exercise():
        store = EmailStore(tmp_path)
        stop, tail, loop = asyncio.Event(), asyncio.Event(), asyncio.get_running_loop()
        pages = []
        class Paged:
            def poll(self, store, **_):
                pages.append(len(pages) + 1)
                store.put('poll_health', {'ok': True, 'more_changes': len(pages) < 3})
                if len(pages) == 3:
                    loop.call_soon_threadsafe(tail.set)
                return 0  # Baseline pages with no new mail are still owed.
        runtime = EmailRuntime(store, Paged(), FakeHost(), account='fixture', poll_interval=3600)
        task = asyncio.create_task(runtime.run(stop))
        try:
            await asyncio.wait_for(tail.wait(), 8)
            await asyncio.sleep(.05)
            assert pages == [1, 2, 3]
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
    asyncio.run(exercise())
