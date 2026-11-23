"""A comment in a source's stored Message-ID is not ancestry on the wire.

A Message-ID such as "(see <x@…>) <a@…>" names <a@…>; intake and lookup already use that canonical token. Replies
wrote the stored spelling into In-Reply-To and References, so the recipient (and our own emitted-scope parser) read
<x@…> from the comment as the conversation root. The header writer now names the source by its canonical token.
The stored source, queue row and Place keys keep their spelling, and both the source root and the actual emitted
root are still checked before any effect. A legacy multi-ID In-Reply-To value is written as before.
"""
import pytest

from lib.client import MailClient
from lib.mime import build_message, emitted_conversation, parse_message, wire_thread_problem
from lib.runtime import EmailRuntime
from test_binding_core import DeliveryClient, release_hold
from test_reply_identity_compatibility import configure_host, invoke
from test_reply_source_authority import service_for, source
from test_write_targets import host_guard

pytest_plugins = ['test_binding_core']

TOKEN, OTHER = '<a@example.invalid>', '<x@example.invalid>'
ANCESTRY = f'(see {OTHER}) {TOKEN}'
LEGACY = '<a@example.invalid> <b@example.invalid>'


@pytest.mark.parametrize(('stored', 'written'), [(ANCESTRY, TOKEN), (f'{TOKEN} (re {OTHER})', TOKEN),
                                                  (f'{TOKEN} (comment)', TOKEN), (TOKEN, TOKEN), (LEGACY, LEGACY)])
def test_writer_names_the_canonical_token(stored, written):
    wire = build_message(sender='s@example.invalid', recipients=['r@example.invalid'], subject='', body='',
                         in_reply_to=stored, references=[], message_id='<n@example.invalid>')
    assert (str(wire['In-Reply-To']), str(wire['References'])) == (written, written)
    assert wire_thread_problem(stored, []) == ''
    assert emitted_conversation(stored, []) == (TOKEN if written == TOKEN else '<a@example.invalid>')
    # A chain that already names the token is not extended by a second spelling of it.
    chained = build_message(sender='s@example.invalid', recipients=['r@example.invalid'], subject='', body='',
                            in_reply_to=stored, references=['<root@example.invalid>', TOKEN], message_id='<n@example.invalid>')
    if written == TOKEN:
        assert str(chained['References']) == f'<root@example.invalid> {TOKEN}'


@pytest.mark.parametrize('tool', ['email_send', 'email_draft'])
@pytest.mark.parametrize(('emitted_state', 'comment_state'), [('active', 'paused'), ('paused', 'active')])
def test_reply_checks_source_and_actual_emitted_roots_not_the_comment(tmp_path, monkeypatch, core_host, tool,
                                                                     emitted_state, comment_state):
    original = source(ANCESTRY, references=[])
    account, call, store, client = service_for(tmp_path, 'imap', captured=[original])
    drive, adapter = configure_host(monkeypatch, core_host, account,
                                    [(ANCESTRY, 'active'), (TOKEN, emitted_state), (OTHER, comment_state)])
    guard = host_guard(tmp_path, monkeypatch, tool, account, [], drive=drive)
    args, result = invoke(call, tool, {'account_id': account, 'reply_to_message_id': TOKEN, 'to': 'peer@example.invalid',
                                       'subject': 'Re: Source', 'body': 'Answer', 'request_id': 'r'}, guard)
    allowed = emitted_state == 'active'
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, adapter, account=account, account_loader=lambda: {'sending': 'on'})
    if not allowed:
        if tool == 'email_draft' or not result.get('ok'):
            assert not store.queued_message('r') and not client.saved
        else:  # queued, then held at delivery by the emitted scope's Paused Place
            assert store.queued_message('r').reporting['emitted_place_binding']['conversation_id'] == TOKEN
            release_hold(store)
            assert runtime.process_outbox() and not delivery.sent and store.receipt('r')['state'] == 'held'
        return
    assert result['ok'], result
    # The historical source keeps its spelling and Place key; the emitted scope is the token actually written.
    assert args['reply_to_message_id'] == ANCESTRY and args['conversation_id'] == ANCESTRY
    assert args['emitted_conversation_id'] == TOKEN
    if tool == 'email_draft':
        assert client.saved[-1]['reply_to_message_id'] == ANCESTRY
        return
    queued = store.queued_message('r')
    assert queued.in_reply_to == ANCESTRY
    assert queued.reporting['place_binding']['conversation_id'] == ANCESTRY
    assert queued.reporting['emitted_place_binding']['conversation_id'] == TOKEN
    wire = MailClient({}).message(queued)
    assert (str(wire['In-Reply-To']), str(wire['References'])) == (TOKEN, TOKEN)
    assert OTHER not in str(parse_message(wire.as_bytes(), folder='', uid=0)['references'])
    release_hold(store)
    assert runtime.process_outbox() and len(delivery.sent) == 1
    assert store.receipt('r')['state'] == 'completed'


def test_automatic_reply_writes_the_token_and_checks_its_scope(tmp_path, monkeypatch, core_host):
    original = source(ANCESTRY, references=[])
    account, _call, store, _client = service_for(tmp_path, 'imap', captured=[original])
    drive, adapter = configure_host(monkeypatch, core_host, account, [(ANCESTRY, 'active'), (TOKEN, 'paused')])
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, adapter, account=account, account_loader=lambda: {'sending': 'on'})
    captured = store.claim_inbox()
    runtime._reply(captured, ['Answer'], 'one')
    rid = 'presence:' + captured.provider_event_key + ':one'
    queued = store.queued_message(rid)
    assert queued.in_reply_to == ANCESTRY and queued.reporting['emitted_place_binding']['conversation_id'] == TOKEN
    release_hold(store)
    assert runtime.process_outbox() and not delivery.sent and store.receipt(rid)['state'] == 'held'
