"""Every supported message or draft mutation names the conversation intake gave the same message.

Matrix (provider x action): Gmail mark_read, flags, labels, archive, copy, move, trash, untrash, delete and
delete_draft; IMAP mark_read, flags, copy and move; Exchange mark_read, flags, copy, move and trash. Each prepared
call exposes ``message_conversation_id`` (``draft_conversation_id`` for delete_draft) and the same root as
``message_thread_id``, read from the provider now with intake’s parser (whole IMAP headers then full-source fallback;
Gmail raw; Exchange MIME or structured fallback).

The real Host guard then finds a Paused Place stored with conversation and thread (the shape intake creates), a
conversation-only Place, and still the independent folder/label Places; an Active thread changes nothing in the
exact provider request.
"""
from contextlib import contextmanager
from dataclasses import replace

import pytest

from lib.client import MailClient
from lib.ews import _fallback_id
from lib.mime import conversation_root, parse_message
from test_ews_folder_places import MUTATIONS, SOURCE, scene
from test_ews_mailbox import ACCOUNT, names
from test_ews_ownership import legacy_message
from test_write_targets import gmail_for, host_guard, published_targets, tools_for

pytest_plugins = ["test_binding_core"]

ROOT_OF = lambda message: conversation_root(references=message.get('references'), in_reply_to=message.get('in_reply_to'),
                                            message_id=message.get('message_id'))

# Identity shapes: header lines of the selected message (Gmail and IMAP).
SHAPES = {
    'own_id': {'Message-ID': '<own@example.invalid>', 'Subject': 'Receipt'},
    'references': {'Message-ID': '<own@example.invalid>', 'References': '<first@example.invalid> <second@example.invalid>'},
    'headerless': {'Subject': 'No identity header'},
    'damaged': {'Message-ID': 'not an id', 'Subject': 'Damaged'},
    'legacy_multi_reply': {'Message-ID': '<own@example.invalid>', 'In-Reply-To': '<a@example.invalid> <b@example.invalid>'},
}


def thread_place(drive, account, conversation, thread, state):
    from ouroboros import places
    row = places.connect_place(drive, connector='email-presence', provider='email', account_id=account,
                               conversation_id=conversation, state=state)
    if thread:
        places._write(drive, row.place_id, lambda stored: replace(stored, thread_id=thread))


def converge(call, guard, tool, args, writes, limit=4):
    """Follow retry_arguments as a caller does; return (Host refusal or '', final arguments, last response)."""
    response = {}
    for _ in range(limit):
        refusal = guard(args) if guard else ''
        if refusal:
            return refusal, args, response
        response = call(tool, args)
        if response.get('status') != 'needs_arguments':
            return '', args, response
        assert not writes, 'preparation must precede every provider write'
        args = response['retry_arguments']
    raise AssertionError('retry_arguments did not converge')


# --------------------------------------------------------------------------------------------------- Gmail
GMAIL = {  # action -> (tool, selection, current labels, exact (method, path, body))
    'mark_read': ('email_mark_read', {}, ['INBOX', 'UNREAD'],
                  ('POST', '/messages/7b/modify', {'addLabelIds': [], 'removeLabelIds': ['UNREAD']})),
    'flags': ('email_mailbox', {'action': 'flags', 'flags': ['\\Flagged']}, ['INBOX'],
              ('POST', '/messages/7b/modify', {'addLabelIds': ['STARRED'], 'removeLabelIds': []})),
    'labels': ('email_mailbox', {'action': 'labels', 'add_labels': ['Label_1']}, ['INBOX'],
               ('POST', '/messages/7b/modify', {'addLabelIds': ['Label_1'], 'removeLabelIds': []})),
    'archive': ('email_mailbox', {'action': 'archive'}, ['INBOX'],
                ('POST', '/messages/7b/modify', {'addLabelIds': [], 'removeLabelIds': ['INBOX']})),
    'copy': ('email_mailbox', {'action': 'copy', 'destination': 'Public'}, ['INBOX'],
             ('POST', '/messages/7b/modify', {'addLabelIds': ['Label_1'], 'removeLabelIds': []})),
    'move': ('email_mailbox', {'action': 'move', 'folder': 'INBOX', 'destination': 'Public'}, ['INBOX'],
             ('POST', '/messages/7b/modify', {'addLabelIds': ['Label_1'], 'removeLabelIds': ['INBOX']})),
    'trash': ('email_mailbox', {'action': 'trash'}, ['INBOX'], ('POST', '/messages/7b/trash', {})),
    'untrash': ('email_mailbox', {'action': 'untrash'}, ['TRASH'], ('POST', '/messages/7b/untrash', {})),
    'delete': ('email_mailbox', {'action': 'delete'}, ['INBOX'], ('DELETE', '/messages/7b', None)),
    'delete_draft': ('email_mailbox', {'action': 'delete_draft', 'draft_id': 'draft-one'}, ['DRAFT'],
                     ('DELETE', '/drafts/draft-one', None)),
}
GMAIL_HELD_LABEL = {'untrash': 'TRASH', 'delete_draft': 'Drafts'}  # a label each action exposes besides INBOX


def gmail_case(tmp_path, action, shape):
    tool, selection, labels, exact = GMAIL[action]
    headers = SHAPES[shape]
    draft = action == 'delete_draft'
    account, call, writes = gmail_for(tmp_path, message_labels=labels, message_headers=None if draft else headers,
                                      drafts={'draft-one': (labels, headers)} if draft else None)
    args = {'account_id': account, **({'uid': 123, 'uidvalidity': 1} if not draft else {}), **selection}
    return tool, args, exact, account, call, writes


def gmail_intake_root(action, shape):
    """The root GmailClient.read/poll derive: the same format=raw payload through the client's own parser."""
    from test_write_targets import raw_message
    from lib.gmail import GmailClient
    native = 'd1' if action == 'delete_draft' else '7b'
    reader = GmailClient.__new__(GmailClient)
    return ROOT_OF(reader._message({'id': native, 'labelIds': [], 'raw': raw_message(SHAPES[shape])}))


def conversation_key(action):
    return 'draft_conversation_id' if action == 'delete_draft' else 'message_conversation_id'


@pytest.mark.parametrize('shape', sorted(SHAPES))
@pytest.mark.parametrize('action', sorted(GMAIL))
def test_gmail_prepares_the_intake_conversation_and_keeps_the_exact_request(tmp_path, action, shape):
    tool, args, exact, _account, call, writes = gmail_case(tmp_path, action, shape)
    root = gmail_intake_root(action, shape)
    assert root and (shape != 'headerless' or root.startswith('<sha256-'))
    assert shape != 'damaged' or root == ('<gmail-d1@ouroboros.local>' if action == 'delete_draft' else '<gmail-7b@ouroboros.local>')
    _, final, response = converge(call, None, tool, args, writes)
    assert final[conversation_key(action)] == final['message_thread_id'] == root
    assert response.get('ok', True) is not False and writes == [exact]
    # A stale or invented identity is never trusted: it is replaced by the one read now, before any write.
    writes.clear()
    stale = call(tool, {**final, conversation_key(action): '<invented@example.invalid>', 'message_thread_id': '<invented@example.invalid>'})
    assert stale['status'] == 'needs_arguments' and stale['retry_arguments'][conversation_key(action)] == root and not writes


@pytest.mark.parametrize('scenario', ['thread_paused', 'conversation_paused', 'thread_active', 'label_paused_under_active_thread'])
@pytest.mark.parametrize('shape', ['own_id', 'headerless'])
@pytest.mark.parametrize('action', sorted(GMAIL))
def test_gmail_real_host_finds_thread_places_without_hiding_label_places(tmp_path, monkeypatch, action, shape, scenario):
    tool, args, exact, account, call, writes = gmail_case(tmp_path, action, shape)
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active')])
    root = gmail_intake_root(action, shape)
    drive = tmp_path / 'host'
    if scenario == 'thread_paused':
        thread_place(drive, account, root, root, 'paused')
    elif scenario == 'conversation_paused':
        thread_place(drive, account, root, '', 'paused')
    else:
        thread_place(drive, account, root, root, 'active')
        if scenario == 'label_paused_under_active_thread':
            thread_place(drive, account, GMAIL_HELD_LABEL.get(action, 'INBOX'), '', 'paused')
    refusal, final, _ = converge(call, guard, tool, args, writes)
    if scenario == 'thread_active':
        assert not refusal and writes == [exact]
        assert published_targets(tool, final)['message_thread_id'] == [root]
    else:
        assert 'Paused' in refusal and writes == []


# ---------------------------------------------------------------------------------------------------- IMAP
def raw_source(headers):
    return (''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\nBody\r\n').encode('utf-8')


class SourceBox:
    """imaplib-shaped responses for one stored message: header-field peeks, full peeks and recorded writes."""
    capabilities = (b'IMAP4rev1', b'MOVE')

    def __init__(self, raw):
        self.raw, self.writes, self.peeks, self.sessions = raw, [], [], []

    def response(self, key):
        return key, [b'7' if key == 'UIDVALIDITY' else b'9']

    def uid(self, *args):
        if args[0] != 'FETCH':
            self.writes.append(args)
            return 'OK', [b'done']
        self.peeks.append(args[2])
        if 'HEADER.FIELDS' in args[2]:
            wanted = {'message-id', 'in-reply-to', 'references'}
            lines, keep = [], False
            for line in self.raw.split(b'\r\n\r\n', 1)[0].split(b'\r\n'):
                if line[:1] not in (b' ', b'\t'):
                    keep = line.split(b':', 1)[0].strip().lower().decode() in wanted
                if keep:
                    lines.append(line)
            data = b''.join(line + b'\r\n' for line in lines) + b'\r\n'
        elif 'BODY.PEEK[HEADER]' in args[2]:
            data = self.raw.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n'
        else:
            data = self.raw
        return 'OK', [(b'4 (UID 4 BODY[] {%d}' % len(data), data), b')']

    @contextmanager
    def open(self, folder=None, *, readonly=True):
        self.sessions.append((folder, readonly))
        yield self


IMAP = {  # action -> (tool, selection, exact write)
    'mark_read': ('email_mark_read', {}, ('STORE', '4', '+FLAGS.SILENT', '(\\Seen)')),
    'flags': ('email_mailbox', {'action': 'flags', 'flags': ['\\Flagged']}, ('STORE', '4', '+FLAGS.SILENT', '(\\Flagged)')),
    'copy': ('email_mailbox', {'action': 'copy', 'destination': 'Archive'}, ('COPY', '4', '"Archive"')),
    'move': ('email_mailbox', {'action': 'move', 'destination': 'Archive'}, ('MOVE', '4', '"Archive"')),
}
IMAP_SHAPES = {**SHAPES, 'missing': {'Subject': 'Only a subject'}}


def imap_case(tmp_path, action, shape):
    tool, selection, exact = IMAP[action]
    raw = raw_source(IMAP_SHAPES[shape])
    box = SourceBox(raw)
    client = MailClient({})
    client.imap = box.open
    account, call = tools_for(tmp_path, 'imap', client)
    args = {'account_id': account, 'uid': 4, 'uidvalidity': 7, 'folder': 'Legal', **selection}
    # Intake (EmailRuntime.poll -> MailClient.fetch) parses the full source with the same reader.
    root = ROOT_OF(parse_message(raw, folder='Legal', uid=4))
    return tool, args, exact, account, call, box, raw, root


@pytest.mark.parametrize('shape', sorted(IMAP_SHAPES))
@pytest.mark.parametrize('action', sorted(IMAP))
def test_imap_prepares_the_intake_conversation_and_keeps_the_exact_command(tmp_path, action, shape):
    tool, args, exact, _account, call, box, raw, root = imap_case(tmp_path, action, shape)
    if shape in ('headerless', 'damaged', 'missing'):
        import hashlib
        assert root == f'<sha256-{hashlib.sha256(raw).hexdigest()}@ouroboros.local>'
    _, final, response = converge(call, None, tool, args, box.writes)
    assert final['message_conversation_id'] == final['message_thread_id'] == root
    assert response['ok'] and box.writes == [exact]
    # Header lines first; the whole source only when they give no identity. Peeks never set \Seen.
    full = shape in ('headerless', 'damaged', 'missing')
    assert all(peek.startswith('(BODY.PEEK[') for peek in box.peeks)
    assert any('BODY.PEEK[]' in peek for peek in box.peeks) is full


@pytest.mark.parametrize('scenario', ['thread_paused', 'conversation_paused', 'thread_active', 'folder_paused_under_active_thread'])
@pytest.mark.parametrize('shape', ['own_id', 'missing'])
@pytest.mark.parametrize('action', sorted(IMAP))
def test_imap_real_host_finds_thread_places_without_hiding_folder_places(tmp_path, monkeypatch, action, shape, scenario):
    tool, args, exact, account, call, box, _raw, root = imap_case(tmp_path, action, shape)
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active')])
    drive = tmp_path / 'host'
    if scenario == 'thread_paused':
        thread_place(drive, account, root, root, 'paused')
    elif scenario == 'conversation_paused':
        thread_place(drive, account, root, '', 'paused')
    else:
        thread_place(drive, account, root, root, 'active')
        if scenario == 'folder_paused_under_active_thread':
            thread_place(drive, account, 'Archive' if action == 'copy' else 'Legal', '', 'paused')
    refusal, _final, _ = converge(call, guard, tool, args, box.writes)
    if scenario == 'thread_active':
        assert not refusal and box.writes == [exact]
    else:
        assert 'Paused' in refusal and box.writes == []


# ------------------------------------------------------------------------------------------------ Exchange
EWS = {  # action -> (tool, selection, exact SOAP operation)
    'mark_read': ('email_mark_read', {}, 'UpdateItem'),
    'flags': ('email_mailbox', {'action': 'flags', 'flags': ['\\Flagged']}, 'UpdateItem'),
    'copy': ('email_mailbox', {'action': 'copy', 'destination': 'Projects'}, 'CopyItem'),
    'move': ('email_mailbox', {'action': 'move', 'destination': 'Projects'}, 'MoveItem'),
    'trash': ('email_mailbox', {'action': 'trash'}, 'MoveItem'),
}
# The fixture item's MIME carries References <parent>; its transport headers say the same, or are absent
# (a locally created item), in which case the item is read as intake reads it.
EWS_HEADERS = {'transport_headers': [('Message-ID', '&lt;message@example.invalid&gt;'),
                                     ('References', '&lt;parent@example.invalid&gt;')],
               'no_transport_headers': []}
EWS_ROOT = '<parent@example.invalid>'


def ews_case(tmp_path, action, headers):
    server, account, call = scene(tmp_path)
    server.item_headers = EWS_HEADERS[headers]
    tool, selection, operation = EWS[action]
    args = {'account_id': account, 'message_ref': legacy_message(ACCOUNT, 'B-msg'), 'folder': 'Decoy', **selection}
    return server, account, call, tool, args, operation


def ews_writes(server):
    return [name for name in names(server) if name in MUTATIONS]


@pytest.mark.parametrize('headers', sorted(EWS_HEADERS))
@pytest.mark.parametrize('action', sorted(EWS))
def test_exchange_prepares_the_intake_conversation_and_keeps_the_exact_operation(tmp_path, action, headers):
    server, _account, call, tool, args, operation = ews_case(tmp_path, action, headers)
    _, final, response = converge(call, None, tool, args, ews_writes(server))
    assert final['message_conversation_id'] == final['message_thread_id'] == EWS_ROOT
    assert response.get('ok') and ews_writes(server) == [operation]
    mime_reads = [op for op in server.operations if op.find('.//{*}IncludeMimeContent') is not None]
    assert mime_reads  # Every identity uses the same parser/fallback chain as intake.


@pytest.mark.parametrize('scenario', ['thread_paused', 'conversation_paused', 'thread_active', 'folder_paused_under_active_thread'])
@pytest.mark.parametrize('action', sorted(EWS))
def test_exchange_real_host_finds_thread_places_without_hiding_folder_places(tmp_path, monkeypatch, action, scenario):
    server, account, call, tool, args, operation = ews_case(tmp_path, action, 'transport_headers')
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active')])
    drive = tmp_path / 'host'
    if scenario == 'thread_paused':
        thread_place(drive, account, EWS_ROOT, EWS_ROOT, 'paused')
    elif scenario == 'conversation_paused':
        thread_place(drive, account, EWS_ROOT, '', 'paused')
    else:
        thread_place(drive, account, EWS_ROOT, EWS_ROOT, 'active')
        if scenario == 'folder_paused_under_active_thread':
            thread_place(drive, account, SOURCE if action != 'copy' else 'Projects', '', 'paused')
    writes = []
    refusal, _final, _ = converge(call, guard, tool, args, writes)
    if scenario == 'thread_active':
        assert not refusal and ews_writes(server) == [operation]
    else:
        assert 'Paused' in refusal and ews_writes(server) == []


def test_exchange_item_without_any_identity_uses_the_native_fallback_intake_uses():
    import base64
    from email.message import EmailMessage
    from test_ews_mailbox import client
    from test_ews_wire import response
    mail = EmailMessage()
    mail['Subject'] = 'Locally created'
    mail.set_content('Draft text.')
    raw = base64.b64encode(mail.as_bytes()).decode()
    mime = response('GetItem', '<m:Items><t:Message><t:ItemId Id="N1" ChangeKey="k"/><t:ParentFolderId Id="F"/>'
                               f'<t:MimeContent>{raw}</t:MimeContent></t:Message></m:Items>')
    exchange, session = client(mime)
    assert exchange.identity_root('N1') == _fallback_id('N1')
    assert names(session) == ['GetItem']


# ------------------------------------------------------------------------------- Gmail draft replacement
@pytest.mark.parametrize('state', ['paused', 'active'])
def test_gmail_draft_replacement_checks_the_old_thread_place_independently(tmp_path, monkeypatch, core_host, state):
    from lib.service import MailService
    places, drive, adapter = core_host
    old = '<old-root@example.invalid>'
    account, call, writes = gmail_for(tmp_path, drafts={'draft-one': (['DRAFT'], {
        'Message-ID': '<draft-one@example.invalid>', 'References': old})})

    @contextmanager
    def host(_self):
        yield adapter
    monkeypatch.setattr(MailService, '_presence_host', host)
    places.connect_place(drive, connector='email-presence', provider='email', account_id=account, state='active')
    thread_place(drive, account, old, old, state)
    # The new content starts no conversation; only the replaced draft's old thread is held.
    args = {'account_id': account, 'draft_id': 'draft-one', 'to': 'reader@example.invalid', 'subject': 'New',
            'body': 'Rewritten'}
    prepared = call('email_draft', args)
    assert prepared['status'] == 'needs_arguments' and prepared['retry_arguments']['draft_conversation_id'] == old
    result = call('email_draft', prepared['retry_arguments'])
    if state == 'paused':
        assert result['code'] == 'draft_folder_held' and result['folder'] == old and not writes
    else:
        assert result['ok'] and [method for method, _path, _body in writes] == ['PUT']
    # Negative control: the same check without the thread (0.10.11 as reviewed) does not find the thread Place.
    assert adapter.draft_folder_hold(account, old) == ''
    assert bool(adapter.draft_folder_hold(account, old, thread_id=old)) is (state == 'paused')
