"""A reply's conversation comes from its source verified in this mailbox, for every provider.

Before 0.10.11 an IMAP or Exchange reply with caller-supplied references skipped the source lookup; a respelled
Message-ID missed the store; the first raw references element became the Host's conversation; and references
without any source could claim an Active conversation over a Paused account. Now: reply_to_message_id is one
canonical Message-ID (another spelling returns an exact retry), its source is always looked up (store, else the
provider, whose answer must carry exactly that ID), the chain is the source's, supplied references are only
compared, and references without a source are refused. The written reply headers are parsed by the intake reader
before anything is queued or saved. Accepted queue rows replay unchanged.
"""
import json
from email import message_from_bytes, policy
from email.policy import SMTP
from types import SimpleNamespace

import pytest

from lib.client import MailClient
from lib.gmail import GmailClient
from lib.mime import build_message, parse_message
from lib.store import EmailStore
from test_write_targets import host_guard, tools_for

pytest_plugins = ["test_binding_core"]

HELD, ROOT = '<held@example.invalid>', '<held-root@example.invalid>'


class Provider:
    """A provider client that knows only what it is given; every lookup and save is recorded."""

    def __init__(self, known=()):
        self.known, self.lookups, self.saved = {m['message_id']: m for m in known}, [], []
        self.settings = {'EMAIL_USER': 'owner@example.invalid'}

    def find_message(self, message_id, **_):
        self.lookups.append(message_id)
        return self.known.get(message_id)

    def draft(self, **kwargs):
        self.saved.append(kwargs)
        return {'ok': True}

    def draft_targets(self, _draft_id):
        return ['Drafts'], ''

    def draft_destination(self, folder):  # Exchange: the resolved folder handle, its names and the saver
        return 'ews-folder:fixture:drafts', ['drafts'], self.draft


def source(message_id=HELD, references=(ROOT,), **extra):
    return {'message_id': message_id, 'references': list(references), 'in_reply_to': '', 'sender': 'peer@example.invalid',
            'folder': 'INBOX', **extra}


def service_for(tmp_path, provider, *, known=(), captured=()):
    client = Provider(known)
    account, call = tools_for(tmp_path, provider, client)
    store = EmailStore(tmp_path / 'email')
    for uid, message in enumerate(captured, 1):
        store.ingest({'uidvalidity': 1, 'uid': uid, 'subject': 'Source', 'body': 'source', **message})
    return account, call, store, client


def reply(account, **changes):
    return {'account_id': account, 'to': 'peer@example.invalid', 'subject': 'Re: Source', 'body': 'Answer',
            'reply_to_message_id': HELD, 'conversation_id': ROOT, 'thread_id': ROOT, 'request_id': 'r1', **changes}


@pytest.mark.parametrize('provider', ['imap', 'ews', 'gmail'])
@pytest.mark.parametrize('tool', ['email_send', 'email_draft'])
def test_unverifiable_source_is_refused_even_with_caller_references(tmp_path, provider, tool):
    account, call, store, client = service_for(tmp_path, provider)
    with pytest.raises(ValueError, match='Reply source was not found'):
        call(tool, reply(account, references=[ROOT]))
    assert client.lookups == [HELD] and not client.saved and store.status()['outbox'] == {}


@pytest.mark.parametrize('provider', ['imap', 'ews', 'gmail'])
@pytest.mark.parametrize('where', ['captured', 'provider'])
@pytest.mark.parametrize('tool', ['email_send', 'email_draft'])
def test_decoy_references_never_choose_the_conversation(tmp_path, provider, where, tool):
    known = {'captured': {'captured': [source()]}, 'provider': {'known': [source()]}}[where]
    account, call, store, client = service_for(tmp_path, provider, **known)
    decoy = '<decoy@example.invalid>'
    refused = call(tool, reply(account, references=[decoy], conversation_id=decoy, thread_id=decoy))
    assert refused['code'] == 'conversation_identity_mismatch' and not client.saved
    assert store.status()['outbox'] == {}
    retry = refused['retry_arguments']
    assert retry['references'] == [ROOT] and retry['conversation_id'] == retry['thread_id'] == ROOT
    done = call(tool, retry)
    assert done['ok']
    if tool == 'email_send':
        queued = store.queued_message('r1')
        assert queued.in_reply_to == HELD and list(queued.references) == [ROOT]
    else:
        assert client.saved[-1]['reply_to_message_id'] == HELD and client.saved[-1]['references'] == [ROOT]


@pytest.mark.parametrize('spelling', [' <held@example.invalid>', '<held@example.invalid> ', 'Re: <held@example.invalid>'])
def test_respelled_reply_id_returns_the_canonical_retry_and_finds_the_captured_source(tmp_path, spelling):
    account, call, store, client = service_for(tmp_path, 'imap', captured=[source()])
    prepared = call('email_send', reply(account, reply_to_message_id=spelling))
    assert prepared['status'] == 'needs_arguments' and prepared['code'] == 'canonical_reply_source_required'
    assert prepared['retry_arguments']['reply_to_message_id'] == HELD and client.lookups == []
    assert store.status()['outbox'] == {}
    assert call('email_send', prepared['retry_arguments'])['ok']
    assert store.queued_message('r1').in_reply_to == HELD


@pytest.mark.parametrize('value', ['<a@example.invalid> <b@example.invalid>', 'held@example.invalid', '', '<>', 7])
def test_reply_id_must_be_exactly_one_message_id(value):
    from lib.mime import canonical_message_id
    assert canonical_message_id(value) == ''


def test_unbracketed_or_extra_reference_elements_cannot_move_the_root(tmp_path):
    account, call, store, _client = service_for(tmp_path, 'imap', captured=[source()])
    supplied = ['unbracketed-token', ROOT]  # the same token chain as the source, plus noise
    result = call('email_send', reply(account, references=supplied))
    assert result['ok'] and list(store.queued_message('r1').references) == [ROOT]


@pytest.mark.parametrize('tool', ['email_send', 'email_draft'])
def test_references_without_a_reply_source_cannot_claim_an_active_conversation(tmp_path, monkeypatch, tool):
    """The Host matches the claimed Active conversation over the Paused account; the skill refuses the claim."""
    account, call, store, client = service_for(tmp_path, 'imap')
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'paused'), ('<active@example.invalid>', 'active')])
    args = {'account_id': account, 'to': 'anyone@example.invalid', 'subject': 'Hello', 'body': 'New mail',
            'references': ['<active@example.invalid>'], 'conversation_id': '<active@example.invalid>',
            'thread_id': '<active@example.invalid>', 'request_id': 'claim'}
    assert guard(args) == '', 'the Host alone lets this claim through (the reported bypass)'
    with pytest.raises(ValueError, match='references continue a conversation'):
        call(tool, args)
    assert store.status()['outbox'] == {} and not client.saved
    plain = {k: v for k, v in args.items() if k not in ('references', 'conversation_id', 'thread_id')}
    assert 'Paused' in guard(plain), 'without the claim the Paused account holds the new mail'


def test_ordinary_reply_after_email_read_is_one_call_without_provider_lookup(tmp_path):
    account, call, store, client = service_for(tmp_path, 'imap')
    store.remember_read({**source(), 'uid': 9, 'uidvalidity': 1})
    assert call('email_send', reply(account))['ok'] and client.lookups == []
    queued = store.queued_message('r1')
    assert (queued.in_reply_to, list(queued.references)) == (HELD, [ROOT])


def test_accepted_queue_row_replays_without_a_new_lookup(tmp_path):
    account, call, store, client = service_for(tmp_path, 'imap')
    # A row accepted by an earlier version, with references this version would not derive.
    store.enqueue_outbox(request_id='old', to=['peer@example.invalid'], subject='Re: Source', body='Answer',
                         in_reply_to='<gone@example.invalid>', references=['<legacy@example.invalid>'],
                         reporting={'sender': 'owner@example.invalid'})
    replay = call('email_send', reply(account, request_id='old', reply_to_message_id='<gone@example.invalid>',
                                      references=['<legacy@example.invalid>'], conversation_id='<legacy@example.invalid>',
                                      thread_id='<legacy@example.invalid>'))
    assert replay['ok'] and replay['deduplicated'] and client.lookups == []


def test_legacy_multi_id_in_reply_to_keeps_its_place_key_and_writes_the_same_ids(tmp_path):
    """A captured source without References whose In-Reply-To holds two IDs is keyed by that whole string."""
    legacy = '<a@example.invalid> <b@example.invalid>'
    account, call, store, _client = service_for(tmp_path, 'imap', captured=[
        {'message_id': HELD, 'references': [], 'in_reply_to': legacy, 'sender': 'peer@example.invalid', 'folder': 'INBOX'}])
    item = store.claim_inbox()
    assert item.thread_key == legacy  # the existing conversation key, unchanged
    prepared = call('email_send', reply(account, conversation_id='', thread_id=''))
    assert prepared['status'] == 'needs_arguments'
    retry = prepared['retry_arguments']
    assert retry['conversation_id'] == retry['thread_id'] == legacy and retry['references'] == [legacy]
    assert call('email_send', retry)['ok']
    queued = store.queued_message('r1')
    wire = message_from_bytes(MailClient({}).message(queued).as_bytes(policy=SMTP), policy=policy.default)
    assert str(wire['In-Reply-To']) == HELD
    assert str(wire['References']).split() == ['<a@example.invalid>', '<b@example.invalid>', HELD]


def test_written_reply_headers_are_parsed_back_by_the_intake_reader():
    from lib.mime import wire_thread_problem
    assert wire_thread_problem(HELD, [ROOT]) == ''
    assert wire_thread_problem('', []) == ''
    assert wire_thread_problem(HELD, ['<a@example.invalid> <b@example.invalid>']) == ''
    assert wire_thread_problem(HELD, [ROOT + '\nBcc: hidden@example.invalid'])  # not a header we can write back


@pytest.mark.parametrize('provider_kind', ['imap', 'gmail'])
def test_provider_lookup_accepts_only_the_exact_message_id(provider_kind):
    """IMAP HEADER search is a substring match and Gmail's query is a search: candidates are compared exactly."""
    decoy = {'uid': 5, 'uidvalidity': 7, 'message_id': '<held@example.invalid.decoy>', 'message_ref': 'gmail:5'}
    exact = {'uid': 6, 'uidvalidity': 7, 'message_id': HELD, 'message_ref': 'gmail:6'}
    reads = []
    if provider_kind == 'imap':
        client = MailClient({})
        client.search = lambda **kwargs: {'messages': list(pages.pop(0))}
        client.read = lambda *, uid, **_: reads.append(uid) or ({**exact} if uid == 6 else {**decoy})
    else:
        client = GmailClient.__new__(GmailClient)
        client.search = lambda **kwargs: {'messages': list(pages.pop(0))}
        client.read = lambda *, message_ref, **_: reads.append(message_ref) or ({**exact} if message_ref == 'gmail:6' else {**decoy})
    pages = [[decoy, exact], [decoy]]
    assert client.find_message(HELD)['message_id'] == HELD and len(reads) == 2
    assert client.find_message(HELD) is None


@pytest.mark.parametrize(('body', 'html_body', 'body_type', 'plain', 'html'), [
    # The reviewed defect: body_type=html with both fields used to drop the explicit body from both parts.
    ('Thank you.', '<p>Thanks, in HTML.</p>', 'html', 'Thank you.', '<p>Thanks, in HTML.</p>'),
    ('Thank you.', '<p>Thanks, in HTML.</p>', 'plain', 'Thank you.', '<p>Thanks, in HTML.</p>'),
    ('<p>Only <b>markup</b></p>', '', 'html', 'Only markup', '<p>Only <b>markup</b></p>'),
    ('', '<p>Rendered</p>', 'html', 'Rendered', '<p>Rendered</p>'),
    ('Plain only', '', 'plain', 'Plain only', None),
])
def test_explicit_plain_body_is_never_silently_replaced(body, html_body, body_type, plain, html):
    built = build_message(sender='owner@example.invalid', recipients=['peer@example.invalid'], subject='S', body=body,
                          html_body=html_body, body_type=body_type)
    wire = message_from_bytes(built.as_bytes(policy=SMTP), policy=policy.default)
    parts = {part.get_content_type(): part.get_content().strip() for part in wire.walk() if not part.is_multipart()}
    assert parts['text/plain'] == plain and parts.get('text/html') == html
    parsed = parse_message(built.as_bytes(policy=SMTP), folder='', uid=0)
    assert parsed['body'].strip() == plain
