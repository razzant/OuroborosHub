"""A Gmail draft's id selects the resource; its system label selects the write scope."""
import pytest

from test_write_targets import gmail_for, host_guard, published_targets


@pytest.mark.parametrize('label', ['DRAFT', 'Drafts'])
@pytest.mark.parametrize('state', ['paused', 'silent', 'active'])
def test_delete_draft_checks_both_folder_spellings_before_delete(tmp_path, monkeypatch, label, state):
    account, call, writes = gmail_for(tmp_path)
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account, [('', 'active'), (label, state)])
    args = dict(account_id=account, action='delete_draft', draft_id='draft-one',
                label_names=['Elsewhere'], remove_labels=['INBOX'])
    assert not guard(args)
    prepared = call('email_mailbox', args)
    assert prepared['status'] == 'needs_arguments' and not writes
    retry = prepared['retry_arguments']
    assert retry['draft_id'] == 'draft-one'
    targets = published_targets('email_mailbox', retry)
    assert targets['label_names'] == ['Drafts'] and targets['remove_labels'] == ['DRAFT']
    if state == 'active':
        assert not guard(retry)
        call('email_mailbox', retry)
        assert writes == [('DELETE', '/drafts/draft-one', None)]
    else:
        assert state.title() in guard(retry) and not writes


@pytest.mark.parametrize('invalid_id', ['', '../messages/abc'])
def test_invalid_draft_id_never_deletes_a_message(tmp_path, invalid_id):
    account, call, writes = gmail_for(tmp_path)
    with pytest.raises(ValueError, match='draft_id'):
        call('email_mailbox', dict(account_id=account, action='delete_draft', draft_id=invalid_id))
    assert not writes
