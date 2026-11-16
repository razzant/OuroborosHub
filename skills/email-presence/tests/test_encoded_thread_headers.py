"""Intake, retained syntax and reply consumers read encoded threading headers the same way.

An encoded word outside comments is decoded as intake always decoded it; one inside a comment or quoted string
stays comment text and never becomes ancestry. Old decoded-only Exchange text is unknown where decoding could
have moved a comment boundary.
"""
from xml.etree import ElementTree as ET

import pytest

from lib.mime import conversation_root, emitted_conversation, parse_message, reply_chain
from lib.retained import UndecidableThreadSyntax
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_binding_core import DeliveryClient, release_hold
from test_reply_identity_compatibility import configure_host, invoke
from test_reply_source_authority import service_for
from test_write_targets import host_guard

pytest_plugins = ['test_binding_core']
HELD, ROOT, DECOY = '<held@example.invalid>', '<root@example.invalid>', '<decoy@example.invalid>'
MID = '<child@example.invalid>'
HELD_B64 = '=?utf-8?b?PGhlbGRAZXhhbXBsZS5pbnZhbGlkPg==?='
COMMENT_DECOY = '(=?utf-8?q?x=29_=3Cdecoy=40example=2Einvalid=3E_=28?=)'  # decodes to "(x) <decoy> ("
QUOTED_DECOY = '" =?utf-8?q?=22_=3Cdecoy=40example=2Einvalid=3E_=22?= "'


def raw(header):
    return (f'From: peer@example.invalid\r\nTo: owner@example.invalid\r\nMessage-ID: {MID}\r\n'
            f'{header}\r\nSubject: Example\r\n\r\nBody').encode()


@pytest.mark.parametrize(('header', 'references', 'in_reply_to', 'emitted'), [
    (f'References: {HELD_B64}', [HELD], '', HELD),
    (f'References: {HELD}', [HELD], '', HELD),
    ('References: =?utf-8?q?<held@exa?= =?utf-8?q?mple.invalid>?=', [HELD], '', HELD),
    (f'References: (=?utf-8?q?Caf=C3=A9?=) {ROOT}', [ROOT], '', ROOT),
    (f'References: {COMMENT_DECOY} {ROOT}', [ROOT], '', ROOT),
    (f'References: {QUOTED_DECOY} {ROOT}', [ROOT], '', ROOT),
    (f'In-Reply-To: {HELD_B64}', [], HELD, HELD),
    (f'In-Reply-To: {COMMENT_DECOY} {ROOT}', [], ROOT, ROOT),
    # An existing key keeps intake's decoded text; only its emitted root is the actual token.
    (f"In-Reply-To: {ROOT} (=?utf-8?q?Fred=27s?= message)", [], f"{ROOT} (Fred's message)", ROOT),
])
def test_intake_and_retained_reply_read_the_same_ancestry(header, references, in_reply_to, emitted):
    message = parse_message(raw(header), folder='INBOX', uid=1)
    assert (message['references'], message['in_reply_to']) == (references, in_reply_to)
    projected = {key: value for key, value in message.items() if key != 'thread_headers'}
    chain = reply_chain(message)
    assert chain == reply_chain(projected) == (references or [in_reply_to])
    assert emitted_conversation(MID, chain) == emitted
    root = conversation_root(references=references, in_reply_to=in_reply_to, message_id=MID)
    assert DECOY not in [*chain, root, emitted]
    assert message['thread_headers'][header.split(':')[0].lower().replace('-', '_')] == header.split(': ', 1)[1]


def staged(store, header, *, old_references=None):
    message = parse_message(raw(header), folder='INBOX', uid=1, include_attachment_data=True)
    message.pop('thread_headers')  # a capture from before thread_headers: only staged MIME keeps the syntax
    message['uidvalidity'] = 1
    if old_references is not None:
        message['references'] = old_references
    store.stage_inbound_attachments(message)
    assert message['source_artifact']['path']
    store.ingest({'subject': 'Source', 'body': 'source', **message})
    return message


def test_staged_encoded_reference_keeps_its_scope_and_accepted_reply(tmp_path):
    store = EmailStore(tmp_path)
    source = staged(store, f'References: {HELD_B64}')
    assert reply_chain(source) == [HELD]
    assert store.historical_message_roots({'folder': 'INBOX', 'uidvalidity': 1, 'uid': 1}) == []
    store.enqueue_outbox(request_id='accepted', recipients=['peer@example.invalid'], subject='Re: Example',
                         body='Answer', in_reply_to=MID, references=[HELD])
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, object(), account='a', account_loader=lambda: {'sending': 'on'})
    assert store.retained_outbox_hold(store.queued_message('accepted')) == ''
    assert runtime.process_outbox() and len(delivery.sent) == 1


def test_staged_encoded_comment_ancestry_stays_held_and_historical(tmp_path):
    store = EmailStore(tmp_path)
    staged(store, f'References: {COMMENT_DECOY} {ROOT}', old_references=[DECOY, ROOT])  # the old decoded projection
    assert store.historical_message_roots({'folder': 'INBOX', 'uidvalidity': 1, 'uid': 1}) == [DECOY]
    store.enqueue_outbox(request_id='accepted', recipients=['peer@example.invalid'], subject='Re: Example',
                         body='Answer', in_reply_to=MID, references=[DECOY, ROOT])
    before = store.queued_message('accepted')
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, object(), account='a', account_loader=lambda: {'sending': 'on'})
    for _ in range(2):
        release_hold(store)
        assert runtime.process_outbox() and not delivery.sent
    after = store.queued_message('accepted')
    assert (after.message_id, after.references, after.recipients, after.body) == (
        before.message_id, before.references, before.recipients, before.body)
    assert store.receipt('accepted')['state'] == 'held' and store.receipt('accepted')['attempts'] == 0


class Capture:
    def __init__(self):
        self.calls = []

    def capture_binding(self, account, conversation, thread_id='', require_place=False, match=None):
        if match is not None:
            # The reply's envelope address, bound exactly; it is no conversation scope of this test.
            assert (match, thread_id) == ('exact_casefold', '')
            return {'supported': True, 'allowed': True, 'binding': {'conversation_id': conversation, 'place_id': ''}}
        self.calls.append((conversation, thread_id))
        return {'supported': True, 'allowed': True, 'binding': {'conversation_id': conversation, 'thread_id': thread_id}}


@pytest.mark.parametrize(('header', 'root'), [(f'References: {HELD_B64}', HELD),
                                              (f'References: {COMMENT_DECOY} {ROOT}', ROOT)])
def test_automatic_reply_writes_and_binds_intake_ancestry(tmp_path, header, root):
    store = EmailStore(tmp_path)
    message = parse_message(raw(header), folder='INBOX', uid=1)
    store.ingest({'uidvalidity': 1, 'subject': 'Example', 'body': 'source', **message})
    item = store.inbox_event('INBOX', 1, 1)
    assert item.thread_key == root
    host = Capture()
    runtime = EmailRuntime(store, object(), host, account='a')
    runtime.remember_reply_targets(item)  # as process_inbox does before submitting the event
    runtime._reply(item, ['Answer'], 'final')
    queued = store.queued_message(f'presence:{item.provider_event_key}:final')
    assert queued.references == (root,) and queued.in_reply_to == MID
    assert host.calls == [(root, root)]  # no separate emitted scope: the reply continues intake's conversation
    assert 'emitted_place_binding' not in store.receipt(queued.request_id)['delivery_reporting']


@pytest.mark.parametrize('state', ['paused', 'active'])
def test_send_reply_to_encoded_reference_uses_its_conversation(tmp_path, monkeypatch, core_host, state):
    source = parse_message(raw(f'References: {HELD_B64}'), folder='INBOX', uid=1)
    account, call, store, _ = service_for(tmp_path, 'imap', captured=[source])
    drive, _ = configure_host(monkeypatch, core_host, account, [(HELD, state), (MID, 'active')])
    guard = host_guard(tmp_path, monkeypatch, 'email_send', account, [], drive=drive)
    args, result = invoke(call, 'email_send', dict(account_id=account, to='peer@example.invalid', body='Answer',
                          reply_to_message_id=MID, request_id='encoded-reply'), guard)
    assert args['conversation_id'] == HELD and not args.get('emitted_conversation_id')
    if state == 'paused':
        assert 'Paused' in result['refusal'] and store.queued_message('encoded-reply') is None
    else:
        assert store.queued_message('encoded-reply').references == (HELD,)


def legacy_ews(display_references):
    # A 0.10.x structured Exchange capture: decoded display text and address syntax, no raw threading syntax.
    return {'folder': 'ews:inbox', 'uid': 1, 'uidvalidity': -1, 'message_id': MID, 'source_format': 'ews_properties',
            'message_ref': 'ews:old', 'provider_event_key': 'ews:old', 'sender': 'peer@example.invalid',
            'recipients': ['owner@example.invalid'], 'cc': [], 'reply_to': [], 'address_names': {},
            'references': [DECOY, ROOT], 'in_reply_to': '', 'subject': 's', 'body': 'Original',
            'headers': {'references': [display_references]}}


@pytest.mark.parametrize(('display', 'chain'), [(f'{HELD} {ROOT}', [HELD, ROOT]), (f'{ROOT} (trailing note)', [ROOT])])
def test_old_exchange_display_text_is_read_where_decoding_cannot_matter(display, chain):
    assert reply_chain(legacy_ews(display)) == chain


def test_old_exchange_display_text_after_a_comment_is_unknown(tmp_path):
    source = legacy_ews(f'(x) {DECOY} () {ROOT}')  # perhaps the decoded form of an encoded comment
    with pytest.raises(UndecidableThreadSyntax, match='read the original again'):
        reply_chain(source)
    store = EmailStore(tmp_path)
    store.ingest(source)
    assert store.historical_message_roots({'message_ref': 'ews:old'}) == [DECOY]  # its stored scope is still checked
    store.enqueue_outbox(request_id='accepted', recipients=['peer@example.invalid'], subject='Re: s', body='b',
                         in_reply_to=MID, references=[DECOY, ROOT])
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, object(), account='a', account_loader=lambda: {'sending': 'on'})
    assert runtime.process_outbox() and not delivery.sent
    assert store.receipt('accepted')['state'] == 'held' and store.receipt('accepted')['attempts'] == 0
    assert store.queued_message('accepted').references == (DECOY, ROOT)


def test_exchange_structured_read_uses_raw_threading_syntax(monkeypatch):
    from lib.ews import EwsClient
    from lib.ews_wire import NS
    from test_ews_mailbox import ACCOUNT
    from test_ews_wire import Session
    rows = {'Message-ID': MID, 'References': HELD_B64, 'In-Reply-To': f'{COMMENT_DECOY} {ROOT}'}
    headers = ''.join(f'<t:InternetMessageHeader HeaderName="{name}">{value.replace("<", "&lt;").replace(">", "&gt;")}'
                      '</t:InternetMessageHeader>' for name, value in rows.items())
    item = ET.fromstring(f'<t:Message xmlns:t="{NS["t"]}"><t:ItemId Id="native"/><t:Subject>s</t:Subject>'
                         f'<t:InternetMessageHeaders>{headers}</t:InternetMessageHeaders></t:Message>')
    client = EwsClient(ACCOUNT, session=Session())
    monkeypatch.setattr(client, '_get_item', lambda native, shape: item)
    message = client._structured_read('native')
    assert (message['references'], message['in_reply_to']) == ([HELD], ROOT)
    assert message['thread_headers'] == {'references': HELD_B64, 'in_reply_to': f'{COMMENT_DECOY} {ROOT}'}
    assert reply_chain(message) == [HELD] and message['headers']['references'] == ['<held@example.invalid>']
