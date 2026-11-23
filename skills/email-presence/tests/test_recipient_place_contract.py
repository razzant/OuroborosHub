"""Skill-side r5/r6 consumer rules, without a core: reply_targets producer and comparison, stored match modes,
legacy/corrupt rows, and the shipped manifest's groups.

The fake Host below only records what the transport asks and answers fixed facts; it is NOT the core's target
matcher. Exact matching, ranking and Safety facts are qualified against the real core in
test_recipient_place_scopes.py and the joint run.
"""
import json
from types import SimpleNamespace

import pytest

from lib.host_adapter import (LEGACY_REPLY_HELD, PLACE_MATCH, RECIPIENT_PLACES, _BINDINGS_CORRUPT, _TARGETS_DIFFER,
                              automatic_reply_targets, binding_modes, email_presence_event, reply_target_rows,
                              valid_reply_targets)
from lib.mime import parse_message
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_declared_target_preparation import MANIFEST

BOB = 'bob@example.invalid'
ROWS = [{'conversation': BOB, 'effect': 'write', 'match': 'exact_casefold'}]


class Host:
    """Records bind/check calls; every Place answer is Active (or unbound for exact binds)."""
    def __init__(self):
        self.binds, self.checks = [], []
    def capture_binding(self, account, conversation, thread_id='', require_place=False, match=None):
        self.binds.append((conversation, thread_id, match))
        place = '' if match else 'account-place'
        return {'supported': True, 'allowed': True, 'state': 'unbound' if match else 'active', 'place_id': place,
                'binding': {'version': 1, 'conversation_id': conversation, 'thread_id': thread_id, 'place_id': place}}
    def binding_hold(self, binding, account, conversation, thread_id='', match=None):
        self.checks.append((conversation, thread_id, match))
        return ''


class Transport:
    settings = {'EMAIL_USER': 'owner@example.invalid'}
    def __init__(self):
        self.sent = []
    def message(self, item):
        from lib.client import MailClient
        return MailClient(self.settings).message(item)
    def delivery(self):
        transport = self
        class Session:
            def __enter__(self):
                return transport
            def __exit__(self, *_):
                return False
        return Session()
    def transmit(self, _session, message, recipients, **_):
        self.sent.append(list(recipients))
        return {}, {'transport': 'offline'}


def capture(store, *, sender=BOB, reply_to='', uid=1):
    raw = (f'From: {sender}\r\nTo: owner@example.invalid\r\n' + (f'Reply-To: {reply_to}\r\n' if reply_to else '')
           + f'Message-ID: <q{uid}@example.invalid>\r\nSubject: Question\r\n\r\nBody').encode()
    store.ingest({'uidvalidity': 1, **parse_message(raw, folder='INBOX', uid=uid)})
    return store.inbox_event('INBOX', 1, uid)


def runtime_for(tmp_path):
    store, host, transport = EmailStore(tmp_path), Host(), Transport()
    return store, host, transport, EmailRuntime(store, transport, host, account='mailbox',
                                                account_loader=lambda: {'sending': 'on'})


def outbox(store):
    with store._connect() as db:
        return [dict(row) for row in db.execute('SELECT request_id,state,last_error,reporting_json FROM outbox')]


# --- producer: event.message.reply_targets ------------------------------------------------------------------

@pytest.mark.parametrize('sender,reply_to,expected', [
    ('Bob@Example.INVALID', '', [BOB]),
    ('alice@example.invalid', 'Bob <Bob@Example.INVALID>, list@example.invalid', [BOB, 'list@example.invalid']),
])
def test_event_carries_the_actual_automatic_reply_envelope(tmp_path, sender, reply_to, expected):
    item = capture(EmailStore(tmp_path), sender=sender, reply_to=reply_to)
    event = email_presence_event(item, account_id='mailbox', places=True)
    assert event['message']['reply_targets'] == automatic_reply_targets(item) == [
        {'conversation': key, 'effect': 'write', 'match': 'exact_casefold'} for key in expected]
    assert valid_reply_targets(event['message']['reply_targets'])


def test_event_without_an_addressable_sender_says_no_targets(tmp_path):
    store = EmailStore(tmp_path)
    store.ingest({'uid': 1, 'uidvalidity': 1, 'folder': 'INBOX', 'message_id': '<q>', 'sender': 'MAILER-DAEMON',
                  'subject': 'Bounce', 'body': 'x'})
    assert email_presence_event(store.inbox_event('INBOX', 1, 1), account_id='m')['message']['reply_targets'] == []


@pytest.mark.parametrize('value,valid', [
    ([], True), (ROWS, True), ([{**ROWS[0], 'thread': ''}], True),
    (None, False), ({}, False), ([BOB], False), ([{**ROWS[0], 'effect': 'message'}], False),
    ([{**ROWS[0], 'match': 'alias'}], False), ([{**ROWS[0], 'match': []}], False),
    ([{**ROWS[0], 'match': {}}], False), ([{**ROWS[0], 'conversation': ''}], False),
    ([{**ROWS[0], 'account': 'x'}], False), ([{'conversation': BOB, 'effect': 'write'}], False),
    ([{**ROWS[0], 'thread': 4}], False)])
def test_reply_targets_shape(value, valid):
    assert valid_reply_targets(value) is valid


# --- enqueue: the same anchor's targets, never another message's ---------------------------------------------

def test_wake_event_targets_recorded_at_submission_release_the_reply(tmp_path):
    store, host, transport, runtime = runtime_for(tmp_path)
    item = capture(store)
    runtime.remember_reply_targets(item)
    runtime._reply(item, ['Answer'], 'final')
    (row,) = outbox(store)
    reporting = json.loads(row['reporting_json'])
    assert row['state'] == 'pending' and reporting['reply_targets'] == ROWS
    assert reporting[PLACE_MATCH] == {'place_binding': 'scope', RECIPIENT_PLACES: 'exact_casefold'}
    assert [r['key'] for r in reporting[RECIPIENT_PLACES]['rows']] == [BOB]
    assert host.binds[-1] == (BOB, '', 'exact_casefold')
    assert runtime.process_outbox() and transport.sent == [[BOB]]
    # The conversation binding is checked as bound (scope: no match argument), the recipient with its own mode;
    # once before the provider session opens and again inside it.
    assert host.checks == [('<q1@example.invalid>', '<q1@example.invalid>', None), (BOB, '', 'exact_casefold')] * 2


def test_output_without_any_targets_is_held_with_its_words(tmp_path):
    store, host, transport, runtime = runtime_for(tmp_path)
    item = capture(store)
    runtime._reply(item, ['Prepared before'], 'final')
    (row,) = outbox(store)
    assert (row['state'], row['last_error']) == ('retry', LEGACY_REPLY_HELD)
    assert 'reply_targets' not in json.loads(row['reporting_json'])
    with store._connect() as db:
        db.execute('UPDATE outbox SET available_at=0')
    assert runtime.process_outbox() and transport.sent == []
    assert store.receipt(row['request_id'])['last_error'] == LEGACY_REPLY_HELD


@pytest.mark.parametrize('targets', [None, [], [{**ROWS[0], 'conversation': 'eve@example.invalid'}],
                                     [{**ROWS[0], 'match': 'nonsense'}]])
def test_selected_anchor_targets_must_equal_its_envelope(tmp_path, targets):
    store, host, transport, runtime = runtime_for(tmp_path)
    item = capture(store)
    runtime.remember_reply_targets(item)  # the wake event's targets are never borrowed for an anchored output
    message = {'folder': 'INBOX', 'uidvalidity': 1, 'imap_uid': 1, 'message_id': item.message_id,
               **({'reply_targets': targets} if targets is not None else {})}
    runtime._reply(item, ['Answer'], 'output:x', anchor={'source_event_id': item.provider_event_key, 'message': message})
    (row,) = outbox(store)
    expected = LEGACY_REPLY_HELD if targets is None else _TARGETS_DIFFER
    assert (row['state'], row['last_error']) == ('retry', expected)


# --- delivery: modes, legacy and corrupt rows ----------------------------------------------------------------

def test_delivery_compares_the_accepted_targets_with_the_envelope(tmp_path):
    store, host, transport, runtime = runtime_for(tmp_path)
    item = capture(store)
    runtime.remember_reply_targets(item)
    runtime._reply(item, ['Answer'], 'final')
    with store._connect() as db:
        db.execute("UPDATE outbox SET reporting_json=json_set(reporting_json,'$.reply_targets[0].conversation',"
                   "'eve@example.invalid')")
    assert runtime.process_outbox() and transport.sent == []
    assert store.receipt(outbox(store)[0]['request_id'])['last_error'] == _TARGETS_DIFFER


@pytest.mark.parametrize('reporting', [
    {'recipient_place_bindings': {'version': 1, 'scopes': []}},          # an unpublished candidate's format
    {RECIPIENT_PLACES: {'match': 'exact_casefold', 'rows': []}},          # bindings without their modes
    {PLACE_MATCH: {'place_binding': 'scope'}},                            # a mode without its binding
    {PLACE_MATCH: {}, 'place_binding': {'place_id': ''}},                 # a binding without its mode
    {PLACE_MATCH: {'place_binding': 'alias'}, 'place_binding': {'place_id': ''}},
    {PLACE_MATCH: {RECIPIENT_PLACES: 'exact_casefold'}, RECIPIENT_PLACES: {'match': 'scope', 'rows': []}},
    *[{PLACE_MATCH: {RECIPIENT_PLACES: 'exact_casefold'}, RECIPIENT_PLACES: value}
      for value in (None, 4, [], 'invalid', {})],
])
def test_incomplete_new_format_row_is_corrupt_never_legacy(tmp_path, reporting):
    with pytest.raises(ValueError):
        binding_modes(reporting)
    store, host, transport, runtime = runtime_for(tmp_path)
    store.enqueue_outbox(request_id='tool', recipients=[BOB], body='x', reporting={'version': 0, **reporting})
    assert runtime.process_outbox() and transport.sent == []
    assert store.receipt('tool')['last_error'] == _BINDINGS_CORRUPT and host.checks == host.binds == []


def test_legacy_tool_row_is_checked_by_fresh_exact_lookups_and_never_rewritten(tmp_path):
    store, host, transport, runtime = runtime_for(tmp_path)
    store.enqueue_outbox(request_id='old', to=['Bob@Example.INVALID'], cc=['carol@example.invalid'], body='x',
                         reporting={'version': 0, 'place_binding': {'place_id': 'p'}})
    before = store.queued_message('old')
    assert binding_modes(before.reporting) is None
    assert runtime.process_outbox() and transport.sent == [['Bob@Example.INVALID', 'carol@example.invalid']]
    assert host.checks[0][2] is None  # the legacy conversation binding: absent mode, exactly as before
    assert host.binds == [(BOB, '', 'exact_casefold'), ('carol@example.invalid', '', 'exact_casefold')] * 2
    after = store.queued_message('old').reporting  # delivery adds only its wire/receipt facts, never bindings
    assert after['place_binding'] == before.reporting['place_binding']
    assert RECIPIENT_PLACES not in after and PLACE_MATCH not in after


# --- manifest ------------------------------------------------------------------------------------------------

def tool(name):
    return next(row for row in MANIFEST['tools'] if row['name'] == name)


def test_manifest_groups():
    assert MANIFEST['plugin_api'] == '2.5'
    send = tool('email_send')
    assert 'text' not in send and send['conversation'] == 'conversation_id' and send['thread'] == 'thread_id'
    assert send['target'] == ['account_id', 'to', 'cc', 'bcc']  # audience: never destination scopes
    assert {(g['conversation'], g['effect'], g.get('match', 'scope')) for g in send['touches']} == {
        ('emitted_conversation_id', 'message', 'scope'), ('to', 'write', 'exact_casefold'),
        ('cc', 'write', 'exact_casefold'), ('bcc', 'write', 'exact_casefold')}
    draft = tool('email_draft')
    assert not {'to', 'cc', 'bcc'} & {g['conversation'] for g in draft['touches']}  # a draft delivers nothing
    assert all(g['effect'] == 'write' and g['match'] == 'exact' for g in draft['touches'])
    for name in ('email_mailbox', 'email_mark_read'):
        row = tool(name)
        assert 'thread' not in row and 'message_conversation_id' not in row['target']
        assert {'conversation': 'message_conversation_id', 'thread': 'message_thread_id', 'effect': 'write'} in row['touches']
    assert 'touches' not in tool('email_settings')
    for row in MANIFEST['tools']:
        for group in row.get('touches', ()):
            assert group.get('match', 'scope') == 'scope' or group['effect'] in ('write', 'read')


def test_rich_send_has_no_text_argument():
    registered = {}
    api = SimpleNamespace(register_tool=lambda name, handler, **kw: registered.setdefault(name, kw))
    from lib.service import register_tools
    register_tools(api, SimpleNamespace(**{n: (lambda **_: None) for n in (
        'send', 'search', 'read', 'mark_read', 'folders', 'mailbox', 'draft', 'receipt', 'test_connection',
        'list_accounts', 'settings_read', 'settings_write')}))
    assert 'text' not in registered['email_send']['schema']['properties']


def test_a_tool_send_is_never_taken_for_an_automatic_reply(tmp_path):
    store, host, transport, runtime = runtime_for(tmp_path)
    store.enqueue_outbox(request_id='presence:chosen-by-a-caller', recipients=[BOB], body='x',
                         reporting={'version': 0, 'origin': {'kind': 'tool'}})
    assert runtime.process_outbox() and transport.sent == [[BOB]]
