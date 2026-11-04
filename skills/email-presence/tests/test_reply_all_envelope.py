"""The public Reply All retry is the actual, editable envelope of one RFC conversation."""
import json
from contextlib import contextmanager
from email import message_from_bytes, policy
from types import SimpleNamespace

import jsonschema
import pytest

from lib.accounts import AccountStore
from lib.client import MailClient
from lib.mime import reply_all_recipients
from lib.runtime import EmailRuntime
from lib.service import MailService, register_tools
from test_binding_core import DeliveryClient
from test_conversation_preparation import context
from test_write_targets import host_guard, published_targets

pytest_plugins = ["test_binding_core"]

ROOT = "<root@local.invalid>"
SOURCE = "<source@local.invalid>"
OWNER = "owner@example.invalid"
ALIAS = "alias@example.invalid"
PEER = "peer@example.invalid"
PAUSED = "paused@example.invalid"
BLIND = "blind@example.invalid"


def registered(service, tool):
    entries = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **kw: entries.update({name: (handler, kw['schema'])})), service)
    handler, schema = entries[tool]
    def call(args):
        jsonschema.validate(args, schema)
        return json.loads(handler(**args))
    return call


def test_replying_to_own_sent_mail_preserves_its_primary_and_copy_groups():
    original = {'sender': 'On behalf of me <ALIAS@example.invalid>', 'recipients': [PEER, PAUSED],
                'cc': [OWNER, PEER, BLIND], 'bcc': ['undisclosed@example.invalid']}
    assert reply_all_recipients(original, [OWNER, 'My identity <alias@example.invalid>']) == {
        'to': [PEER, PAUSED], 'cc': [BLIND]}


@pytest.mark.parametrize('kind', ['send', 'draft'])
def test_registered_retry_projects_actual_recipients_and_preserves_rfc_policy(tmp_path, monkeypatch, core_host, kind):
    places, drive, adapter = core_host
    original = {'message_id': SOURCE, 'references': [ROOT], 'sender': 'from@example.invalid',
                'reply_to': [PEER], 'recipients': [OWNER, 'Mail alias <ALIAS@example.invalid>', PAUSED],
                'cc': [PAUSED, PEER.upper()], 'bcc': ['secret-source@example.invalid']}
    account, service, store, _ = context(tmp_path/'mail', original)
    account['senders'].append(ALIAS)
    AccountStore(tmp_path/'mail').save(account)
    aid = account['id']
    saved, transmitted = [], []
    class WireMail(MailClient):
        def find_message(self, message_id, **_):
            assert message_id == SOURCE
            return original
        @contextmanager
        def imap(self, *_, **__):
            def append(folder, flags, date, wire):
                saved.append(message_from_bytes(wire, policy=policy.default))
                return 'OK', [b'saved']
            yield SimpleNamespace(append=append)
    class WireDelivery(DeliveryClient):
        def transmit(self, session, message, recipients, **opts):
            transmitted.append(message)
            return super().transmit(session, message, recipients, **opts)
    service.mail_client = WireMail
    @contextmanager
    def test_host(_):
        yield adapter
    monkeypatch.setattr(MailService, '_presence_host', test_host)
    service.capture_binding = lambda acc, thread: adapter.capture_binding(acc['id'], thread, thread_id=thread)
    tool = 'email_' + kind
    call = registered(service, tool)
    # An explicitly active RFC retains its send exception even if the entire mailbox
    # is paused. Drafts separately need a writable physical destination.
    places.connect_place(drive, connector='email-presence', provider='email', account_id=aid,
                         state='paused' if kind == 'send' else 'active')
    rfc = places.connect_place(drive, connector='email-presence', provider='email', account_id=aid,
                               conversation_id=ROOT, state='active')
    places.connect_place(drive, connector='email-presence', provider='email', account_id=aid,
                         conversation_id=PAUSED, state='paused')
    guard = host_guard(tmp_path, monkeypatch, tool, aid, [], drive=drive)
    # Even a fully identified account/thread/folder cannot bypass expansion. An explicit
    # self copy survives automatic self-exclusion; source Bcc must never be reconstructed.
    args = dict(account_id=aid, thread_id=ROOT, conversation_id=ROOT, to='', subject='Answer', body='Hello',
                reply_all=True, reply_to_message_id=SOURCE, cc=[OWNER], bcc=[PAUSED, BLIND])
    if kind == 'draft':
        args['folder'] = 'Drafts'
    assert not guard(args)
    offer = call(args)
    assert offer['status'] == 'needs_arguments' and not saved and store.status()['outbox'] == {}
    retry = offer['retry_arguments']
    assert retry['reply_all'] is False and retry['references'] == [ROOT] and retry['reply_to_message_id'] == SOURCE
    assert retry['to'] == PEER and retry['cc'] == [PAUSED, OWNER] and retry['bcc'] == [BLIND]
    targets = published_targets(tool, retry)
    assert targets['to'] == [PEER] and targets['cc'] == [PAUSED, OWNER] and targets['bcc'] == [BLIND]
    places.edit_place(drive, rfc.place_id, 'set_state', author='owner', state='paused')
    assert 'Paused' in guard(retry)
    assert not saved and store.status()['outbox'] == {}
    places.edit_place(drive, rfc.place_id, 'set_state', author='owner', state='active')
    assert not guard(retry)  # A paused address-named Place is not a contact ban in this RFC.
    assert call(retry)['ok']
    if kind == 'draft':
        assert len(saved) == 1 and not transmitted and store.status()['outbox'] == {}
        assert str(saved[0]['Bcc']) == BLIND  # Private stored draft, never transmitted.
        message = saved[0]
    else:
        queued = store.queued_message(retry['request_id'])
        assert (queued.to, queued.cc, queued.bcc) == ((PEER,), (PAUSED, OWNER), (BLIND,))
        wire = WireDelivery({'EMAIL_USER': OWNER})
        runtime = EmailRuntime(store, wire, adapter, account=aid, account_loader=lambda: account)
        assert runtime.process_outbox() and len(transmitted) == 1
        assert wire.sent[0][1] == [PEER, PAUSED, OWNER, BLIND]
        message = transmitted[0]
        assert 'Bcc' not in message and BLIND not in message.as_string()
    assert str(message['To']) == PEER and str(message['Cc']) == f'{PAUSED}, {OWNER}'
    assert str(message['In-Reply-To']) == SOURCE and ROOT in str(message['References'])


@pytest.mark.parametrize('kind', ['send', 'draft'])
def test_editing_the_single_retry_replaces_automatic_members_even_if_source_changes(tmp_path, kind):
    original = {'message_id': SOURCE, 'references': [ROOT], 'sender': PEER, 'recipients': [OWNER], 'cc': [PAUSED]}
    account, service, store, calls = context(tmp_path, original)
    call = registered(service, 'email_' + kind)
    result = call(dict(to='', body='Hello', subject='Answer', reply_to_message_id=SOURCE, reply_all=True))
    assert result['status'] == 'needs_arguments' and calls == [('read', SOURCE)]
    retry = result['retry_arguments']
    assert retry['account_id'] == account['id'] and retry['conversation_id'] == retry['thread_id'] == ROOT
    assert retry['to'] == PEER and retry['cc'] == [PAUSED] and retry['bcc'] == [] and not retry['reply_all']
    if kind == 'draft':
        assert retry['folder'] == 'Drafts'
    store.remember_read({**original, 'sender': 'later@example.invalid', 'cc': [BLIND]})
    edit = {**retry, 'to': OWNER, 'cc': [], 'bcc': [PEER]}
    assert call(edit)['ok']
    if kind == 'send':
        saved = store.queued_message(retry['request_id'])
        assert (saved.to, saved.cc, saved.bcc) == ((OWNER,), (), (PEER,))
        assert call(edit)['deduplicated']
        original_call = dict(to='', body='Hello', subject='Answer', reply_to_message_id=SOURCE,
                             reply_all=True, request_id=retry['request_id'])
        mismatch = call(original_call)
        assert mismatch['code'] == 'request_id_conflict'
        assert PEER not in json.dumps(mismatch) and store.status()['outbox'] == {'pending': 1}
    else:
        assert (calls[-1][1]['to'], calls[-1][1]['cc'], calls[-1][1]['bcc']) == (OWNER, [], [PEER])


@pytest.mark.parametrize('source_state', ['changed', 'gone'])
def test_queued_reply_replays_original_and_explicit_calls_without_regenerating_members(tmp_path, source_state):
    source = {'message_id': SOURCE, 'references': [ROOT], 'sender': PEER, 'cc': [PAUSED]}
    account, service, store, calls = context(tmp_path, source)
    call = registered(service, 'email_send')
    account['senders'].append(ALIAS)
    AccountStore(tmp_path).save(account)
    extra_to, extra_cc = 'to-extra@example.invalid', 'cc-extra@example.invalid'
    high = dict(to=extra_to, cc=[extra_cc], subject='Answer', body='Original', reply_all=True,
                reply_to_message_id=SOURCE, bcc=[BLIND], request_id='queue-once')
    offer = call(high)
    retry = offer['retry_arguments']
    first = call(retry)
    assert first['ok'] and not first['deduplicated']
    assert 'reply_all_fingerprint' not in first['receipt']['delivery_reporting']
    store.put('read_message:' + SOURCE, {**source, 'sender': 'later@example.invalid', 'cc': []} if source_state=='changed' else None)
    for args in [high, retry]:
        same = call(args)
        assert same['deduplicated'] and same['receipt']['provider_message_id'] == first['receipt']['provider_message_id']
    for key, value in [('to', ''), ('to', 'other@example.invalid'), ('cc', []), ('cc', [PAUSED]),
                       ('bcc', []), ('bcc', [OWNER]), ('body', 'changed'), ('subject', 'other'), ('sender', ALIAS),
                       ('references', ['<other@local.invalid>']), ('reply_to_message_id', '<other@local.invalid>')]:
        conflict = call({**high, key: value})
        assert conflict['code'] == 'request_id_conflict'
        assert BLIND not in json.dumps(conflict)
    assert call({**retry, 'cc': []})['code'] == 'request_id_conflict'
    assert calls == [('read', SOURCE), ('binding', ROOT)]
    queued = store.queued_message(retry['request_id'])
    assert (queued.to, queued.cc, queued.bcc, queued.references) == ((PEER, extra_to), (PAUSED, extra_cc), (BLIND,), (ROOT,))
    assert store.status()['outbox'] == {'pending': 1}


def test_reply_to_self_keeps_a_blank_draft_and_never_promotes_bcc_into_to(tmp_path):
    original = {'message_id': SOURCE, 'references': [ROOT], 'sender': OWNER, 'recipients': [OWNER],
                'bcc': ['hidden-from-source@example.invalid']}
    account, service, store, calls = context(tmp_path, original)
    args = dict(account_id=account['id'], to='', subject='', body='', reply_all=True, reply_to_message_id=SOURCE)
    draft = registered(service, 'email_draft')
    retry = draft(args)['retry_arguments']
    assert (retry['to'], retry['cc'], retry['bcc'], retry['subject'], retry['body']) == ('', [], [], '', '')
    assert draft(retry)['ok'] and calls[-1][0] == 'draft' and calls[-1][1]['to'] == ''
    with pytest.raises(ValueError, match='to .*required'):
        registered(service, 'email_send')({**args, 'body': 'Hello', 'bcc': [BLIND]})
    assert store.status()['outbox'] == {}


@pytest.mark.parametrize('stamp', [None, 'not-a-fingerprint'])
def test_mail_without_reply_all_provenance_replays_explicitly_without_disclosing_old_recipients(tmp_path, stamp):
    account, service, store, calls = context(tmp_path, None)
    call = registered(service, 'email_send')
    explicit = dict(account_id=account['id'], request_id='old', reply_to_message_id=SOURCE,
                    references=[ROOT], thread_id=ROOT, conversation_id=ROOT, subject='Answer', body='Body',
                    to=PEER, cc=[PAUSED], bcc=[BLIND])
    if stamp is None:
        store.enqueue_outbox(request_id='old', in_reply_to=SOURCE, references=[ROOT], subject='Answer',
                             body='Body', to=[PEER], cc=[PAUSED], bcc=[BLIND], reporting={'sender': OWNER})
    else:
        # Replay metadata never selects the audience or blocks a new explicit send.
        assert call({**explicit, 'reply_all_fingerprint': stamp})['ok']
    assert 'reply_all_fingerprint' not in store.queued_message('old').reporting
    implicit = call({**explicit, 'reply_all': True, 'to': '', 'cc': [], 'bcc': []})
    assert not implicit['ok'] and implicit['code'] == 'reply_all_replay_unverifiable'
    assert all(addr not in json.dumps(implicit) for addr in (PEER, PAUSED, BLIND))
    assert call(explicit)['deduplicated']
    assert store.status()['outbox'] == {'pending': 1} and not any(c[0] == 'read' for c in calls)
