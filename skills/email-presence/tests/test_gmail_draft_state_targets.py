"""Deleting or replacing a Gmail draft by draft_id exposes the draft message's current labels and RFC conversation.

drafts.delete permanently deletes the draft's message and drafts.update replaces it: the message leaves every
label it bears and its RFC conversation. A Paused or Silent Place on any of them must refuse before the
DELETE or PUT. The conversation is the root intake uses (References, In-Reply-To, else the draft's own
Message-ID). These targets are Host facts only: an all-Active call makes exactly the provider request the
caller asked for, and decoy targets the caller supplies are cleared rather than trusted.
"""
import base64
from contextlib import contextmanager
from email import message_from_bytes
from email.policy import default

import pytest

from lib.service import MailService
from test_declared_target_preparation import published_targets
from test_write_targets import gmail_for, host_guard

pytest_plugins = ["test_binding_core"]

LABELS = [{'id': 'Label_Secret', 'name': 'Secret'}, {'id': 'Label_1', 'name': 'Public'}]
DRAFTS = {
    'labelled': (['DRAFT', 'Label_Secret'], {'Message-ID': '<labelled@local.invalid>'}),
    'threaded': (['DRAFT'], {'Message-ID': '<reply-draft@local.invalid>', 'In-Reply-To': '<parent@local.invalid>',
                             'References': '<root@local.invalid> <parent@local.invalid>'}),
    'unthreaded': (['DRAFT'], {'Message-ID': '<own@local.invalid>'}),
}
# (draft, held Place conversation_id): every way the old draft's message can sit in a held Place.
HELD = [('labelled', 'Label_Secret'), ('labelled', 'Secret'), ('labelled', 'DRAFT'), ('labelled', 'Drafts'),
        ('threaded', '<root@local.invalid>'), ('unthreaded', '<own@local.invalid>')]
ROOTS = {'labelled': '<labelled@local.invalid>', 'threaded': '<root@local.invalid>', 'unthreaded': '<own@local.invalid>'}


def converge(call, tool, guard, args, writes, limit=4):
    for _ in range(limit):
        refusal = guard(args) if guard else ''
        if refusal:
            return refusal, args, None
        response = call(tool, args)
        if response.get('status') != 'needs_arguments':
            return '', args, response
        assert not writes, 'a preparation step wrote to Gmail'
        args = response['retry_arguments']
    raise AssertionError('retry_arguments did not converge')


def exposed(tool, args):
    return {value for values in published_targets(tool, args).values() for value in values}


@pytest.mark.parametrize(('draft', 'held'), HELD)
@pytest.mark.parametrize('state', ['paused', 'silent'])
def test_delete_draft_refuses_a_held_label_or_conversation_of_the_draft_message(tmp_path, monkeypatch, draft, held, state):
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, drafts=DRAFTS)
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account, [('', 'active'), (held, state)])
    args = {'account_id': account, 'action': 'delete_draft', 'draft_id': draft}
    refusal, final, _ = converge(call, 'email_mailbox', guard, args, writes)
    assert refusal and state.title() in refusal and not writes
    assert held in exposed('email_mailbox', final)


@pytest.mark.parametrize('draft', sorted(DRAFTS))
def test_delete_draft_all_active_deletes_exactly_the_draft_and_clears_decoys(tmp_path, monkeypatch, draft):
    reads = []
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, reads=reads, drafts=DRAFTS)
    # Every target that could appear is Active, including the decoys: an Active decoy must not stay as a target.
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account,
                       [('', 'active'), ('Label_Secret', 'active'), ('Drafts', 'active'), (ROOTS[draft], 'active')])
    args = {'account_id': account, 'action': 'delete_draft', 'draft_id': draft, 'add_labels': ['Label_1'],
            'label_id': 'Label_1', 'destination': 'Public', 'label_names': ['Public'], 'remove_labels': ['INBOX']}
    refusal, final, response = converge(call, 'email_mailbox', guard, args, writes)
    assert not refusal and response is not None
    assert writes == [('DELETE', '/drafts/' + draft, None)]
    assert '/drafts/' + draft in reads  # the draft's current state was read before the write
    labels = DRAFTS[draft][0]
    assert final['remove_labels'] == labels and final['draft_conversation_id'] == ROOTS[draft]
    assert final['add_labels'] == [] and final['label_id'] == '' and final['destination'] == ''
    assert 'Public' not in final['label_names'] and 'Label_1' not in exposed('email_mailbox', final)
    assert {'Drafts', *labels} <= exposed('email_mailbox', final)


def test_a_stale_or_invented_draft_conversation_is_replaced_before_any_delete(tmp_path):
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, drafts=DRAFTS)
    args = {'account_id': account, 'action': 'delete_draft', 'draft_id': 'threaded', 'remove_labels': ['DRAFT'],
            'label_names': ['Drafts'], 'draft_conversation_id': '<unrelated@local.invalid>'}
    prepared = call('email_mailbox', args)
    assert prepared['status'] == 'needs_arguments' and not writes
    assert prepared['retry_arguments']['draft_conversation_id'] == '<root@local.invalid>'
    with pytest.raises(ValueError, match='only to Gmail delete_draft'):
        call('email_mailbox', {'account_id': account, 'action': 'labels', 'uid': 1, 'uidvalidity': 1,
                               'add_labels': ['Label_1'], 'draft_conversation_id': '<root@local.invalid>'})
    assert not writes


@pytest.fixture
def draft_host(tmp_path, monkeypatch, core_host):
    places, drive, adapter = core_host
    @contextmanager
    def host(_self):
        yield adapter
    monkeypatch.setattr(MailService, '_presence_host', host)
    return places, drive


NEW = {'subject': 'Updated', 'body': 'New text', 'to': 'reader@example.invalid'}


@pytest.mark.parametrize(('draft', 'held'), HELD)
@pytest.mark.parametrize('state', ['paused', 'silent'])
def test_draft_update_refuses_when_the_replaced_draft_sits_in_a_held_place(tmp_path, monkeypatch, draft_host, draft,
                                                                            held, state):
    places, drive = draft_host
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, drafts=DRAFTS)
    places.connect_place(drive, connector='email-presence', provider='email', account_id=account, state='active')
    places.connect_place(drive, connector='email-presence', provider='email', account_id=account,
                         conversation_id=held, state=state)
    guard = host_guard(tmp_path, monkeypatch, 'email_draft', account, [], drive=drive)
    args = {'account_id': account, 'draft_id': draft, **NEW}
    refusal, final, response = converge(call, 'email_draft', guard, args, writes)
    assert not writes
    if refusal:
        assert state.title() in refusal
    else:
        assert response['code'] == 'draft_folder_held' and state in response['error']
    assert held in {*final.get('folder_names', []), final.get('draft_conversation_id'), final.get('folder')}


@pytest.mark.parametrize('draft', sorted(DRAFTS))
def test_draft_update_all_active_saves_exactly_the_callers_new_content(tmp_path, monkeypatch, draft_host, draft):
    places, drive = draft_host
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, drafts=DRAFTS)
    for room in ('', 'Label_Secret', 'Drafts', ROOTS[draft]):
        places.connect_place(drive, connector='email-presence', provider='email', account_id=account,
                             conversation_id=room, state='active')
    guard = host_guard(tmp_path, monkeypatch, 'email_draft', account, [], drive=drive)
    args = {'account_id': account, 'draft_id': draft, **NEW}
    refusal, final, response = converge(call, 'email_draft', guard, args, writes)
    assert not refusal and response['ok'] and response['draft_id'] == draft
    assert final['draft_conversation_id'] == ROOTS[draft]
    assert final['folder'] == 'DRAFT' and set(final['folder_names']) >= {'Drafts'} | set(DRAFTS[draft][0]) - {'DRAFT'}
    assert len(writes) == 1 and writes[0][:2] == ('PUT', '/drafts/' + draft)
    body = writes[0][2]
    assert set(body) == {'message'} and set(body['message']) == {'raw'}  # no labels, no old thread
    raw = body['message']['raw']
    saved = message_from_bytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)), policy=default)
    assert saved['Subject'] == 'Updated' and saved['To'] == 'reader@example.invalid'
    assert saved.get_content().strip() == 'New text'
    # The old draft's conversation is a Host target only; it never threads the saved content.
    assert saved['In-Reply-To'] is None and saved['References'] is None
    assert saved['Message-ID'] != DRAFTS[draft][1]['Message-ID']


def test_draft_conversation_id_is_refused_without_a_gmail_draft_id(tmp_path):
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, drafts=DRAFTS)
    with pytest.raises(ValueError, match='draft_conversation_id'):
        call('email_draft', {'account_id': account, 'folder': 'DRAFT', 'folder_names': ['Drafts'],
                             'draft_conversation_id': '<root@local.invalid>', **NEW})
    assert not writes
