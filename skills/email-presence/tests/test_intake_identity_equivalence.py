"""Malformed and lossy metadata must not change intake's authority. All sources and effects are synthetic."""
import base64
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import pytest

from lib.client import MailClient
from lib.ews import EwsClient
from lib.ews_wire import NS
from lib.gmail import GmailClient
from lib.mime import parse_message
from test_ews_mailbox import ACCOUNT, item
from test_ews_ownership import SharedStores, legacy_message
from test_ews_wire import Session, response
from test_mutation_identity_authority import (GMAIL, IMAP, EWS, ROOT_OF, SourceBox, converge, thread_place, ews_writes)
from test_write_targets import gmail_for, host_guard, tools_for

pytest_plugins = ['test_binding_core']

# Both malformed forms stop Python's parser before the provider's projected identity headers.
RAW = {
    'ordinary': b'Message-ID: <own@example.invalid>\r\nReferences: <root@example.invalid>\r\n\r\nbody',
    'obsolete': b'X-Mailer : legacy\r\nMessage-ID: <own@example.invalid>\r\nReferences: <root@example.invalid>\r\n\r\nbody',
    'colonless': b'broken header\r\nMessage-ID: <own@example.invalid>\r\nReferences: <root@example.invalid>\r\n\r\nbody',
    'encoded_reply': b'Message-ID: <own@example.invalid>\r\nIn-Reply-To: =?utf-8?q?=3Croot=40example.invalid=3E?=\r\n\r\nbody',
    'encoded_references': b'Message-ID: <own@example.invalid>\r\nReferences: =?utf-8?q?=3Croot=40example.invalid=3E?=\r\n\r\nbody',
    'duplicate_references': b'Message-ID: <own@example.invalid>\r\nReferences: none\r\nReferences: <root@example.invalid>\r\n\r\nbody',
    'eightbit_id': b'Message-ID: <caf\xe9@example.invalid>\r\n\r\nbody',
    'encoded_id': b'Message-ID: =?utf-8?q?=3Croot=40example.invalid=3E?=\r\n\r\nbody',
    'eightbit_reply': b'Message-ID: <own@example.invalid>\r\nIn-Reply-To: <caf\xe9@example.invalid>\r\n\r\nbody',
    'metadata_newline': b'Message-ID: <own@example.invalid>\r\nSubject: folded\r\n value\r\nReferences: <root@example.invalid>\r\n\r\nbody',
}


def metadata(shape):
    if shape in ('eightbit_id', 'encoded_id'):
        return {'Message-ID': '<café@example.invalid>'}
    if shape == 'eightbit_reply':
        return {'Message-ID': '<own@example.invalid>', 'In-Reply-To': '<café@example.invalid>'}
    if shape == 'metadata_newline':
        return {'Message-ID': '<own@example.invalid>', 'Subject': 'folded\r\n\r\nvalue', 'References': '<root@example.invalid>'}
    if shape == 'duplicate_references':
        return {'Message-ID': '<own@example.invalid>', 'References': '<root@example.invalid>'}
    return {'Message-ID': '<own@example.invalid>', 'References': '<root@example.invalid>'}


def guard_for(tmp_path, monkeypatch, tool, account, root, state):
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active')])
    thread_place(tmp_path / 'host', account, root, root, state)
    return guard


@pytest.mark.parametrize('shape', RAW)
@pytest.mark.parametrize('action', GMAIL)
@pytest.mark.parametrize('state', ['active', 'paused', 'silent'])
def test_gmail_raw_identity_at_actual_host_guard(tmp_path, monkeypatch, shape, action, state):
    raw = base64.urlsafe_b64encode(RAW[shape]).decode()
    monkeypatch.setattr('test_write_targets.raw_message', lambda *a, **kw: raw)
    tool, selection, labels, exact = GMAIL[action]
    draft = action == 'delete_draft'
    account, call, writes = gmail_for(tmp_path, message_labels=labels, message_headers=metadata(shape),
                                      drafts={'draft-one': (labels, metadata(shape))} if draft else None)
    native = 'd1' if draft else '7b'
    root = ROOT_OF(GmailClient.__new__(GmailClient)._message({'id': native, 'raw': raw}))
    guard = guard_for(tmp_path, monkeypatch, tool, account, root, state)
    args = {'account_id': account, **selection, **({} if draft else {'uid': 123, 'uidvalidity': 1})}
    refusal, final, _ = converge(call, guard, tool, args, writes)
    assert final['message_thread_id'] == root
    assert final['draft_conversation_id' if draft else 'message_conversation_id'] == root
    assert writes == ([exact] if state == 'active' else [])
    assert (state.title() in refusal) if state != 'active' else not refusal


@pytest.mark.parametrize('shape', RAW)
@pytest.mark.parametrize('action', IMAP)
@pytest.mark.parametrize('state', ['active', 'paused', 'silent'])
def test_imap_whole_header_at_actual_host_guard(tmp_path, monkeypatch, shape, action, state):
    tool, selection, exact = IMAP[action]
    box = SourceBox(RAW[shape])
    client = MailClient({}); client.imap = box.open
    account, call = tools_for(tmp_path, 'imap', client)
    root = ROOT_OF(parse_message(RAW[shape], folder='Legal', uid=4))
    guard = guard_for(tmp_path, monkeypatch, tool, account, root, state)
    refusal, final, _ = converge(call, guard, tool, {'account_id': account, 'uid': 4, 'uidvalidity': 7,
                               'folder': 'Legal', **selection}, box.writes)
    assert final['message_conversation_id'] == final['message_thread_id'] == root
    assert box.writes == ([exact] if state == 'active' else [])
    assert (state.title() in refusal) if state != 'active' else not refusal
    assert all('BODY.PEEK' in command for command in box.peeks)


class IdentityExchange(SharedStores):
    def __init__(self, shape):
        super().__init__(); self.shape = shape; self.item_folder = 'inbox'
        self.item_headers = [(k, escape(v)) for k, v in metadata(shape).items()]
        if shape.startswith('encoded_'):
            name = 'In-Reply-To' if shape == 'encoded_reply' else 'References'
            self.item_headers = [('Message-ID', '&lt;own@example.invalid&gt;'),
                                 (name, '=?utf-8?q?=3Croot=40example.invalid=3E?=')]
        if shape == 'duplicate_references':
            self.item_headers = [('References', 'none'), ('References', '&lt;root@example.invalid&gt;')]
        if shape == 'missing_transport':
            self.item_headers = [('Subject', 'only subject')]

    def post(self, *args, **kwargs):
        op = ET.fromstring(kwargs['data']).find('s:Body', NS)[0]
        mime = op.find('.//t:IncludeMimeContent', NS) is not None
        structured = op.find('.//t:FieldURI[@FieldURI="item:Body"]', NS) is not None
        if op.tag.endswith('}GetItem') and (mime or (structured and self.shape == 'conversion_failure')):
            if mime and self.shape == 'conversion_failure':
                xml = response('GetItem', klass='Error', code='ErrorMimeContentConversionFailed')
            else:
                node = ET.fromstring('<r xmlns:t="'+NS['t']+'">'+item('B-msg', mime=True)+'</r>')[0]
                node.find('t:ParentFolderId', NS).set('Id', 'B-inbox')
                if structured:
                    node.remove(node.find('t:MimeContent', NS))
                    headers = ET.SubElement(node, '{'+NS['t']+'}InternetMessageHeaders')
                    ET.SubElement(headers, '{'+NS['t']+'}InternetMessageHeader', HeaderName='Subject').text = 'transport only'
                else:
                    raw = RAW.get(self.shape, RAW['ordinary'])
                    node.find('t:MimeContent', NS).text = base64.b64encode(raw).decode()
                xml = response('GetItem', '<m:Items>'+ET.tostring(node, encoding='unicode')+'</m:Items>')
            self.responses.append(xml)
            return Session.post(self, *args, **kwargs)
        return super().post(*args, **kwargs)


@pytest.mark.parametrize('shape', [*RAW, 'missing_transport', 'conversion_failure'])
@pytest.mark.parametrize('action', EWS)
@pytest.mark.parametrize('state', ['active', 'paused', 'silent'])
def test_exchange_intake_fallback_at_actual_host_guard(tmp_path, monkeypatch, shape, action, state):
    server = IdentityExchange(shape)
    client = EwsClient({**ACCOUNT, 'address': 'b@example.invalid'}, session=server)
    account, call = tools_for(tmp_path, 'ews', client, account_id=client.account['id'])
    original = client._read_scoped(message_ref='ews:B-msg')
    root = ROOT_OF(original)
    assert (original.get('source_format') == 'ews_properties') == (shape == 'conversion_failure')
    server.operations.clear()
    tool, selection, operation = EWS[action]
    guard = guard_for(tmp_path, monkeypatch, tool, account, root, state)
    refusal, final, _ = converge(call, guard, tool, {'account_id': account,
        'message_ref': legacy_message(ACCOUNT, 'B-msg'), 'folder': 'Decoy', **selection}, [])
    assert final['message_conversation_id'] == final['message_thread_id'] == root
    assert ews_writes(server) == ([operation] if state == 'active' else [])
    assert (state.title() in refusal) if state != 'active' else not refusal
    if state == 'active':
        mutation = next(op for op in server.operations if op.tag.endswith('}'+operation))
        assert mutation.find('.//t:ItemId', NS).get('Id') == 'B-msg'
        if action in ('copy', 'move', 'trash'):
            assert mutation.find('m:ToFolderId/t:FolderId', NS).get('Id') == ('B-deleteditems' if action == 'trash' else 'B-projects')


def test_gmail_search_metadata_cannot_inject_headers():
    client = GmailClient.__new__(GmailClient)
    view = client._message({'id': '7b', 'payload': {'headers': [
        {'name': 'Subject', 'value': 'hello\r\n\r\nReferences: <injected@example.invalid>'},
        {'name': 'X\nReferences', 'value': '<injected@example.invalid>'},
        {'name': 'Message-ID', 'value': '<own@example.invalid>'},
        {'name': 'References', 'value': '<root@example.invalid>'}]}}, headers_only=True)
    assert view['references'] == ['<root@example.invalid>'] and view['message_id'] == '<own@example.invalid>'
    assert 'References:' in view['subject']


@pytest.mark.parametrize('shape', RAW)
@pytest.mark.parametrize('state', ['active', 'paused', 'silent'])
def test_replaced_gmail_draft_uses_raw_identity_and_independent_thread_hold(tmp_path, monkeypatch, core_host, shape, state):
    from contextlib import contextmanager
    from lib.service import MailService
    places, drive, adapter = core_host
    @contextmanager
    def host(self):
        yield adapter
    monkeypatch.setattr(MailService, '_presence_host', host)
    raw = base64.urlsafe_b64encode(RAW[shape]).decode()
    monkeypatch.setattr('test_write_targets.raw_message', lambda *a, **kw: raw)
    account, call, writes = gmail_for(tmp_path, drafts={'draft-one':(['DRAFT'],metadata(shape))})
    root = ROOT_OF(GmailClient.__new__(GmailClient)._message({'id':'d1','raw':raw}))
    places.connect_place(drive,connector='email-presence',provider='email',account_id=account,state='active')
    thread_place(drive,account,root,root,state)
    guard = host_guard(tmp_path,monkeypatch,'email_draft',account,[],drive=drive)
    refusal, final, result = converge(call,guard,'email_draft',{'account_id':account,'draft_id':'draft-one',
                     'to':'peer@example.invalid','subject':'Updated','body':'New text'},writes)
    assert final['draft_conversation_id'] == root
    if state == 'active':
        assert not refusal and result['ok'] and len(writes) == 1
        method,path,body = writes[0]
        assert (method,path) == ('PUT','/drafts/draft-one') and set(body) == {'message'}
        saved = parse_message(base64.urlsafe_b64decode(body['message']['raw']+'==='),folder='',uid=0)
        assert saved['subject'] == 'Updated' and saved['body'].strip() == 'New text' and not saved['in_reply_to']
    else:
        assert not writes and (state.title() in refusal or state in result.get('error',''))
