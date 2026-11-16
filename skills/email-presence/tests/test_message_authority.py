"""All reachable Gmail message mutations expose live RFC and folder authority without changing the wire request."""
import pytest
from lib.service import MailService
from test_write_targets import gmail_for, host_guard
from test_gmail_draft_state_targets import converge, exposed

ROOT = '<held@example.invalid>'
HEADERS = {'Message-ID': '<self@example.invalid>', 'In-Reply-To': '<parent@example.invalid>',
           'References': ROOT + ' <parent@example.invalid>'}
CASES = [
    ('email_mailbox', {'action': 'delete'}, 'DELETE', '/messages/7b', None),
    ('email_mailbox', {'action': 'trash'}, 'POST', '/messages/7b/trash', {}),
    ('email_mailbox', {'action': 'untrash'}, 'POST', '/messages/7b/untrash', {}),
    ('email_mailbox', {'action': 'flags', 'flags': [r'\Seen']}, 'POST', '/messages/7b/modify', {'addLabelIds': [], 'removeLabelIds': ['UNREAD']}),
    ('email_mailbox', {'action': 'labels', 'add_labels': ['STARRED']}, 'POST', '/messages/7b/modify', {'addLabelIds': ['STARRED'], 'removeLabelIds': []}),
    ('email_mailbox', {'action': 'archive'}, 'POST', '/messages/7b/modify', {'addLabelIds': [], 'removeLabelIds': ['INBOX']}),
    ('email_mailbox', {'action': 'copy', 'destination': 'STARRED'}, 'POST', '/messages/7b/modify', {'addLabelIds': ['STARRED'], 'removeLabelIds': []}),
    ('email_mailbox', {'action': 'move', 'destination': 'STARRED'}, 'POST', '/messages/7b/modify', {'addLabelIds': ['STARRED'], 'removeLabelIds': ['INBOX']}),
    ('email_mark_read', {}, 'POST', '/messages/7b/modify', {'addLabelIds': [], 'removeLabelIds': ['UNREAD']}),
]

@pytest.mark.parametrize('tool,selection,method,path,body', CASES)
@pytest.mark.parametrize('state', ['paused', 'silent', 'active'])
@pytest.mark.parametrize('by_ref', [False, True])
@pytest.mark.parametrize('held', [ROOT, 'Secret'])
def test_message_mutations_share_live_authority(tmp_path, monkeypatch, tool, selection, method, path, body, state, by_ref, held):
    account, call, writes = gmail_for(tmp_path, message_labels=['DRAFT', 'Label_Secret', 'INBOX'],
                                    label_rows=[{'id': 'Label_Secret', 'name': 'Secret'}], message_headers=HEADERS)
    source = {'message_ref': MailService.wrap_ref({'id': account}, {'message_ref': 'gmail:7b'})} if by_ref else {'uid': 123, 'uidvalidity': 1}
    args = {'account_id': account, **source, **selection}
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active'), (held, state)])
    refusal, final, _ = converge(call, tool, guard, args, writes)
    if state == 'active':
        assert not refusal and writes == [(method, path, body)]
    else:
        assert state.title() in refusal and writes == []
    assert final['message_conversation_id'] == ROOT
    assert {ROOT, 'DRAFT', 'Drafts', 'Label_Secret', 'Secret', 'INBOX'} <= exposed(tool, final)

@pytest.mark.parametrize('headers,root', [({'Message-ID': '<self@example.invalid>'}, '<self@example.invalid>'),
    ({'Message-ID': '<self@example.invalid>', 'In-Reply-To': ROOT}, ROOT), (HEADERS, ROOT)])
def test_canonical_retry_rechecks_conversation_and_replaces_stale_target(tmp_path, headers, root):
    current = dict(headers)
    account, call, writes = gmail_for(tmp_path, message_labels=[], message_headers=current)
    args = {'account_id': account, 'action': 'delete', 'uid': 123, 'uidvalidity': 1,
            'message_conversation_id': '<decoy@example.invalid>'}
    first = call('email_mailbox', args)['retry_arguments']
    assert first['message_conversation_id'] == root and not writes
    current.clear()
    current['Message-ID'] = '<changed@example.invalid>'
    second = call('email_mailbox', first)['retry_arguments']
    assert second['message_conversation_id'] == '<changed@example.invalid>' and not writes
    call('email_mailbox', second)
    assert writes == [('DELETE', '/messages/7b', None)]
