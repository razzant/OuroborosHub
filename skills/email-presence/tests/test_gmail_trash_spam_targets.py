"""Gmail messages.modify into or out of Trash/Spam exposes every current label to the real Host guard first.

The selected message bears INBOX, a custom label and DRAFT. Entering or leaving Trash or Spam hides or
reveals it under all of them, so a Paused or Silent Place on any of those labels (by ID or current name, and
Drafts for DRAFT) must refuse the prepared call before any write. Those labels are authority targets only:
the Gmail request body stays exactly the caller's intended change.
"""
import pytest

from test_declared_target_preparation import published_targets
from test_write_targets import gmail_for, host_guard

pytest_plugins = ["test_binding_core"]

LABELS = [{'id': 'Label_Secret', 'name': 'Secret'}, {'id': 'Label_1', 'name': 'Public'}]
CURRENT = ['INBOX', 'Label_Secret', 'DRAFT']
IN_TRASH, IN_SPAM = ['Label_Secret', 'DRAFT', 'TRASH'], ['Label_Secret', 'DRAFT', 'SPAM']
MODIFY, NO_BODY = '/messages/7b/modify', {}

# (caller selection, the message's current labelIds, the one permitted POST path, its exact body)
CASES = [
    ({'action': 'flags', 'flags': ['\\Deleted']}, CURRENT, MODIFY, {'addLabelIds': ['TRASH'], 'removeLabelIds': []}),
    ({'action': 'flags', 'flags': ['\\Deleted'], 'remove_flags': True}, IN_TRASH, MODIFY,
     {'addLabelIds': [], 'removeLabelIds': ['TRASH']}),
    ({'action': 'flags', 'flags': ['\\Seen', '\\Flagged', '\\Deleted']}, CURRENT, MODIFY,
     {'addLabelIds': ['STARRED', 'TRASH'], 'removeLabelIds': ['UNREAD']}),
    ({'action': 'labels', 'add_labels': ['TRASH']}, CURRENT, MODIFY, {'addLabelIds': ['TRASH'], 'removeLabelIds': []}),
    ({'action': 'labels', 'remove_labels': ['TRASH']}, IN_TRASH, MODIFY, {'addLabelIds': [], 'removeLabelIds': ['TRASH']}),
    ({'action': 'labels', 'add_labels': ['SPAM']}, CURRENT, MODIFY, {'addLabelIds': ['SPAM'], 'removeLabelIds': []}),
    ({'action': 'labels', 'remove_labels': ['SPAM'], 'add_labels': ['Label_1']}, IN_SPAM, MODIFY,
     {'addLabelIds': ['Label_1'], 'removeLabelIds': ['SPAM']}),
    ({'action': 'copy', 'destination': 'TRASH'}, CURRENT, MODIFY, {'addLabelIds': ['TRASH'], 'removeLabelIds': []}),
    ({'action': 'copy', 'destination': 'SPAM'}, CURRENT, MODIFY, {'addLabelIds': ['SPAM'], 'removeLabelIds': []}),
    ({'action': 'move', 'folder': 'INBOX', 'destination': 'TRASH'}, CURRENT, MODIFY,
     {'addLabelIds': ['TRASH'], 'removeLabelIds': ['INBOX']}),
    ({'action': 'move', 'folder': 'INBOX', 'destination': 'SPAM'}, CURRENT, MODIFY,
     {'addLabelIds': ['SPAM'], 'removeLabelIds': ['INBOX']}),
    ({'action': 'move', 'folder': 'TRASH', 'destination': 'INBOX'}, IN_TRASH, MODIFY,
     {'addLabelIds': ['INBOX'], 'removeLabelIds': ['TRASH']}),
    ({'action': 'move', 'folder': 'SPAM', 'destination': 'INBOX'}, IN_SPAM, MODIFY,
     {'addLabelIds': ['INBOX'], 'removeLabelIds': ['SPAM']}),
    ({'action': 'trash'}, CURRENT, '/messages/7b/trash', NO_BODY),
    ({'action': 'untrash'}, IN_TRASH, '/messages/7b/untrash', NO_BODY),
]
IDS = ['flag-deleted', 'unflag-deleted', 'mixed-flags', 'add-trash', 'remove-trash', 'add-spam', 'spam-to-label',
       'copy-trash', 'copy-spam', 'move-inbox-trash', 'move-inbox-spam', 'move-out-of-trash', 'move-out-of-spam',
       'trash', 'untrash']
# Every current label, by ID and by current name (Drafts for DRAFT), reaches the Host guard.
EXPOSED = {'Label_Secret', 'Secret', 'DRAFT', 'Drafts'}


def exposed(final):
    return {value for values in published_targets('email_mailbox', final).values() for value in values}


def converge(call, guard, args, writes, limit=4):
    """Follow retry_arguments as an agent would; return (refusal or '', final args, responses)."""
    responses = []
    for _ in range(limit):
        refusal = guard(args) if guard else ''
        if refusal:
            return refusal, args, responses
        response = call('email_mailbox', args)
        responses.append(response)
        if response.get('status') != 'needs_arguments':
            return '', args, responses
        assert not writes, 'a preparation step wrote to Gmail'
        args = response['retry_arguments']
    raise AssertionError('retry_arguments did not converge')


@pytest.mark.parametrize(('selection', 'current', 'path', 'body'), CASES, ids=IDS)
def test_trash_and_spam_changes_expose_current_labels_without_changing_the_request(tmp_path, selection, current, path, body):
    reads = []
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, reads=reads, message_labels=current)
    args = {'account_id': account, 'uid': 123, 'uidvalidity': 1, **selection}
    refusal, final, responses = converge(call, None, args, writes)
    assert not refusal and len(responses) == 2  # one preparation, then the actual retry
    assert writes == [('POST', path, body)]  # exactly one write, and no authority label leaks into its body
    assert '/messages/7b' in reads  # current labels were read before the write
    assert EXPOSED | ({'INBOX'} if 'INBOX' in current else set()) <= exposed(final)
    if path == MODIFY:
        # Current labels only extend label_names; the mutation arguments stay what the caller asked for.
        assert EXPOSED <= set(final['label_names'])
        for key, wire in (('add_labels', 'addLabelIds'), ('remove_labels', 'removeLabelIds')):
            if key in final:  # the caller's own list, or exactly the IDs the body changes
                assert final[key] in (selection.get(key), body[wire])
    assert responses[-1].get('status') != 'needs_arguments'


def test_ordinary_label_changes_also_read_the_message_authority(tmp_path):
    reads = []
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, reads=reads, message_labels=CURRENT)
    args = {'account_id': account, 'uid': 123, 'uidvalidity': 1, 'action': 'labels', 'add_labels': ['Label_1']}
    refusal, _final, _ = converge(call, None, args, writes)
    assert not refusal and writes == [('POST', '/messages/7b/modify', {'addLabelIds': ['Label_1'], 'removeLabelIds': []})]
    assert '/messages/7b' in reads


def test_renamed_label_between_calls_needs_a_new_retry_then_converges(tmp_path):
    rows = [dict(row) for row in LABELS]
    account, call, writes = gmail_for(tmp_path, label_rows=rows, message_labels=CURRENT)
    args = {'account_id': account, 'uid': 123, 'uidvalidity': 1, 'action': 'labels', 'add_labels': ['TRASH']}
    prepared = call('email_mailbox', args)['retry_arguments']
    assert 'Secret' in prepared['label_names']
    rows[0]['name'] = 'Secret renamed'
    again = call('email_mailbox', prepared)
    assert again['status'] == 'needs_arguments' and not writes
    assert 'Secret renamed' in again['retry_arguments']['label_names']
    assert 'Secret' not in again['retry_arguments']['label_names']
    done = call('email_mailbox', again['retry_arguments'])
    assert done.get('status') != 'needs_arguments'
    assert writes == [('POST', '/messages/7b/modify', {'addLabelIds': ['TRASH'], 'removeLabelIds': []})]


@pytest.mark.parametrize(('selection', 'current', 'path', 'body'), CASES, ids=IDS)
@pytest.mark.parametrize('held', [('Label_Secret', 'paused'), ('Secret', 'silent'), ('Drafts', 'paused'),
                                  ('DRAFT', 'silent'), ('INBOX', 'paused')])
def test_real_host_guard_refuses_a_paused_or_silent_current_label(tmp_path, monkeypatch, selection, current, path, body,
                                                                  held):
    if held[0] == 'INBOX' and 'INBOX' not in current and 'INBOX' not in str(selection):
        pytest.skip('this message does not bear INBOX')
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, message_labels=current)
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account, [('', 'active'), held])
    args = {'account_id': account, 'uid': 123, 'uidvalidity': 1, **selection}
    refusal, final, _ = converge(call, guard, args, writes)
    assert refusal and held[1].title() in refusal and not writes
    assert held[0] in exposed(final)


@pytest.mark.parametrize(('selection', 'current', 'path', 'body'), CASES, ids=IDS)
def test_real_host_guard_allows_active_labels_with_the_exact_body(tmp_path, monkeypatch, selection, current, path, body):
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, message_labels=current)
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account,
                       [('', 'active'), ('Label_Secret', 'active'), ('Drafts', 'active')])
    args = {'account_id': account, 'uid': 123, 'uidvalidity': 1, **selection}
    refusal, _final, _ = converge(call, guard, args, writes)
    assert not refusal and writes == [('POST', path, body)]


@pytest.mark.parametrize('folder', ['SPAM', 'TRASH', 'INBOX', 'Public'])
def test_spam_and_trash_search_asks_gmail_to_include_them_on_every_page(folder):
    from lib.gmail import GmailClient
    client = GmailClient.__new__(GmailClient)
    client.account = {'id': 'gmail:fixture', 'provider': 'gmail'}
    listed = []

    def request(method, path, params=None, **_):
        if path == '/labels':
            return {'labels': LABELS}
        if path == '/messages':
            listed.append(dict(params))
            return {'messages': [{'id': 'a%d' % len(listed)}], 'nextPageToken': 'next' if len(listed) < 3 else ''}
        return {'id': path.rsplit('/', 1)[-1], 'payload': {'headers': []}, 'labelIds': [folder]}
    client._request = request
    token = ''
    for _ in range(3):
        token = client.search(folder=folder, limit=1, page_token=token)['next_page_token']
    assert not token and len(listed) == 3
    assert [page.get('pageToken') for page in listed] == [None, 'next', 'next']
    expected = 'true' if folder in ('SPAM', 'TRASH') else None
    assert [page.get('includeSpamTrash') for page in listed] == [expected] * 3


# The other of Trash and Spam is a current label like any other: changing one must not hide the other.
OTHER = [
    ({'action': 'labels', 'add_labels': ['TRASH']}, ['INBOX', 'SPAM'], 'SPAM', {'addLabelIds': ['TRASH'], 'removeLabelIds': []}),
    ({'action': 'flags', 'flags': ['\\Deleted']}, ['SPAM', 'Label_Secret'], 'SPAM',
     {'addLabelIds': ['TRASH'], 'removeLabelIds': []}),
    ({'action': 'copy', 'destination': 'TRASH'}, ['SPAM'], 'SPAM', {'addLabelIds': ['TRASH'], 'removeLabelIds': []}),
    ({'action': 'labels', 'remove_labels': ['TRASH']}, ['TRASH', 'SPAM'], 'SPAM', {'addLabelIds': [], 'removeLabelIds': ['TRASH']}),
    ({'action': 'labels', 'add_labels': ['SPAM']}, ['INBOX', 'TRASH'], 'TRASH', {'addLabelIds': ['SPAM'], 'removeLabelIds': []}),
    ({'action': 'move', 'folder': 'SPAM', 'destination': 'INBOX'}, ['SPAM', 'TRASH'], 'TRASH',
     {'addLabelIds': ['INBOX'], 'removeLabelIds': ['SPAM']}),
]


@pytest.mark.parametrize(('selection', 'current', 'other', 'body'), OTHER)
@pytest.mark.parametrize('state', ['paused', 'silent'])
def test_the_other_of_trash_and_spam_is_exposed_and_refused(tmp_path, monkeypatch, selection, current, other, body, state):
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, message_labels=current)
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account, [('', 'active'), (other, state)])
    args = {'account_id': account, 'uid': 123, 'uidvalidity': 1, **selection}
    refusal, final, _ = converge(call, guard, args, writes)
    assert refusal and state.title() in refusal and not writes
    assert other in final['label_names']


@pytest.mark.parametrize(('selection', 'current', 'other', 'body'), OTHER)
def test_the_other_of_trash_and_spam_active_keeps_the_exact_body(tmp_path, monkeypatch, selection, current, other, body):
    account, call, writes = gmail_for(tmp_path, label_rows=LABELS, message_labels=current)
    guard = host_guard(tmp_path, monkeypatch, 'email_mailbox', account, [('', 'active'), (other, 'active')])
    args = {'account_id': account, 'uid': 123, 'uidvalidity': 1, **selection}
    refusal, final, _ = converge(call, guard, args, writes)
    assert not refusal and writes == [('POST', MODIFY, body)]
    assert other in final['label_names']
