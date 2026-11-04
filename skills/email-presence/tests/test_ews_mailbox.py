"""Representative wire exchanges for native EWS item references and mailbox operations."""
import base64
from email.message import EmailMessage

import pytest

from lib.ews import EwsClient
from lib.ews_wire import EwsError, NS
from test_ews_wire import CONFIG, FakeResponse, Session, response

ACCOUNT = {'id': 'ews:mailbox', 'address': 'mailbox@example.invalid', 'sender': 'mailbox@example.invalid', 'config': CONFIG}
NATIVE = 'AA+aB/C='


def client(*replies):
    session = Session(*replies)
    return EwsClient(ACCOUNT, session=session), session


def identity(kind, native, *, parent='I+/=', key='c3RvcmU=', changekey='key'):
    element = 'Message' if kind == 'Item' else kind
    prop = (f'<t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0xffa" PropertyType="Binary"/>'
            f'<t:Value>{key}</t:Value></t:ExtendedProperty>') if key is not None else ''
    return response('Get' + kind, f'<m:{kind}s><t:{element}><t:{kind}Id Id="{native}" ChangeKey="{changekey}"/>'
                    f'<t:ParentFolderId Id="{parent}"/>{prop}</t:{element}></m:{kind}s>')


def proof(kind='Item', native=NATIVE):
    return identity(kind, native), identity('Folder', 'I+/=')


def item(native=NATIVE, *, mime=False, mid=True):
    mail = EmailMessage()
    mail['From'] = 'Иван Петров <sender@example.invalid>'
    mail['To'] = ACCOUNT['address']
    mail['Subject'] = 'Заголовок'
    mail['References'] = '<parent@example.invalid>'
    if mid:
        mail['Message-ID'] = '<message@example.invalid>'
    mail.set_content('Письмо.')
    mail.add_attachment(b'file bytes', maintype='application', subtype='octet-stream', filename='document.bin')
    raw = base64.b64encode(mail.as_bytes()).decode()
    return (f'<t:Message><t:ItemId Id="{native}" ChangeKey="key"/><t:ParentFolderId Id="I+/="/>'
            '<t:DateTimeReceived>2026-10-31T16:45:00Z</t:DateTimeReceived><t:Subject>Заголовок</t:Subject>'
            '<t:From><t:Mailbox><t:Name>Иван Петров</t:Name><t:EmailAddress>sender@example.invalid</t:EmailAddress></t:Mailbox></t:From>'
            + ('<t:InternetMessageId>&lt;message@example.invalid&gt;</t:InternetMessageId>' if mid else '')
            + '<t:IsRead>false</t:IsRead><t:References>&lt;parent@example.invalid&gt;</t:References>'
            + (f'<t:MimeContent>{raw}</t:MimeContent>' if mime else '') + '</t:Message>')


def listing(rows, *, last=True, offset=None):
    return response('FindItem', '<m:RootFolder TotalItemsInView="2" IncludesLastItemInRange="'
                    + str(last).lower() + '"' + (f' IndexedPagingOffset="{offset}"' if offset else '')
                    + '><t:Items>' + rows + '</t:Items></m:RootFolder>')


def names(session):
    return [op.tag.split('}')[-1] for op in session.operations]


def test_folder_auth_names_mailbox_not_login_and_reports_server_clock():
    xml = response('GetFolder', '<m:Folders><t:Folder><t:FolderId Id="I"/></t:Folder></m:Folders>')
    c, s = client(FakeResponse(xml, Date='Wed, 04 Nov 2026 00:00:00 GMT'))
    status = c.test_connection()
    assert status['address'] == ACCOUNT['address'] and status['server_time'] == 1793750400
    assert s.operations[0].findtext('m:FolderIds/t:DistinguishedFolderId/t:Mailbox/t:EmailAddress', namespaces=NS) == ACCOUNT['address']
    assert names(s) == ['GetFolder']


def test_search_paging_opaque_case_sensitive_metadata_then_exact_read_without_marking():
    c, s = client(listing(item(mid=False), last=False, offset=1), listing(item(NATIVE.swapcase())), *proof(),
                  response('GetItem', '<m:Items>' + item(mime=True, mid=False) + '</m:Items>'))
    first = c.search(query='subject:обзор & notes', limit=1)
    second = c.search(query='subject:обзор & notes', limit=1, page_token=first['next_page_token'])
    found = first['messages'][0]
    assert found['message_ref'] == 'ews:' + NATIVE and found['provider_id'] == NATIVE
    assert found['references'] == ['<parent@example.invalid>'] and found['sender_name'] == 'Иван Петров'
    assert 'body' not in found and second['messages'][0]['message_ref'] != found['message_ref']
    assert s.operations[1].find('m:IndexedPageItemView', NS).get('Offset') == '1'
    assert s.operations[0].findtext('m:QueryString', namespaces=NS) == 'subject:обзор & notes'
    assert s.operations[0].find('.//t:IncludeMimeContent', NS) is None
    result = c.read(message_ref=found['message_ref'], include_attachment_data=True)
    assert result['message_id'] == found['message_id'] and result['body'] == 'Письмо.'
    assert result['provider_event_key'] == found['message_ref'] and 'uid' not in result
    assert result['sender'] == 'sender@example.invalid' and result['attachments'][0]['data'] == b'file bytes'
    assert result['flags'] == [] and result['_raw_source']
    assert names(s) == ['FindItem', 'FindItem', 'GetItem', 'GetFolder', 'GetItem']


def test_read_refuses_wrong_returned_item_or_missing_mime():
    c, _ = client(response('GetItem', '<m:Items>' + item(native='Other') + '</m:Items>'), *proof(),
                  response('GetItem', '<m:Items>' + item() + '</m:Items>'))
    with pytest.raises(EwsError, match='ItemIdentityMismatch'):
        c.read(message_ref='ews:' + NATIVE)
    with pytest.raises(EwsError, match='MissingResult'):
        c.read(message_ref='ews:' + NATIVE)


def test_find_message_uses_exact_rfc_restriction_not_aqs_interpolation():
    c, s = client(listing(item()), response('GetItem', '<m:Items>' + item(mime=True) + '</m:Items>'))
    assert c.find_message('<message@example.invalid>')['references'] == ['<parent@example.invalid>']
    assert s.operations[0].find('.//t:Constant', NS).get('Value') == '<message@example.invalid>'
    assert s.operations[0].find('.//t:FieldURI[@FieldURI="message:InternetMessageId"]', NS) is not None
    assert s.operations[0].find('m:QueryString', NS) is None


def test_folder_pages_and_ambiguous_name_are_not_silently_resolved():
    def folders(id_, last):
        return response('FindFolder', f'<m:RootFolder IncludesLastItemInRange="{str(last).lower()}"'
                        + ('' if last else ' IndexedPagingOffset="1"') + '><t:Folders>'
                        + f'<t:Folder><t:FolderId Id="{id_}"/><t:DisplayName>Reports</t:DisplayName></t:Folder>'
                          '<t:CalendarFolder><t:FolderId Id="C"/></t:CalendarFolder></t:Folders></m:RootFolder>')
    c, s = client(folders('F1', False), folders('F2', True), folders('F1', False), folders('F2', True))
    assert [f['id'] for f in c.mailbox(action='list')['folders']] == ['ews-folder:ews:mailbox:F1', 'ews-folder:ews:mailbox:F2']
    with pytest.raises(ValueError, match='unique name'):
        c.mailbox(action='move', message_ref='ews:' + NATIVE, destination='Reports')
    assert names(s) == ['FindFolder'] * 4


@pytest.mark.parametrize('action,operation', [('copy', 'CopyItem'), ('move', 'MoveItem'), ('trash', 'MoveItem')])
def test_move_copy_return_new_reference_only_when_supplied(action, operation):
    checks = (*(() if action == 'trash' else proof('Folder', 'dst')), *proof())
    c, s = client(*checks, response(operation, '<m:Items><t:Message><t:ItemId Id="New+/="/></t:Message></m:Items>'),
                  *checks, response(operation))
    args = dict(action=action, destination='ews-folder:ews:mailbox:dst', message_ref='ews:' + NATIVE)
    assert c.mailbox(**args)['message_ref'] == 'ews:New+/='
    assert not c.mailbox(**args).get('message_ref')
    effects = [op for op in s.operations if op.tag.endswith(operation)]
    assert len(effects) == 2
    assert effects[0].find('m:ItemIds/t:ItemId', NS).get('Id') == NATIVE
    target = effects[0].find('m:ToFolderId', NS)[0]
    assert target.get('Id') == ('deleteditems' if action == 'trash' else 'dst')


def test_create_folder_and_flags_use_exact_target_and_current_changekey():
    c, s = client(response('CreateFolder', '<m:Folders><t:Folder><t:FolderId Id="F+"/></t:Folder></m:Folders>'),
                  *proof(), response('UpdateItem'))
    assert c.mailbox(action='create', folder='INBOX', destination='Tests & more')['id'] == 'ews-folder:ews:mailbox:F+'
    assert s.operations[0].findtext('.//t:DisplayName', namespaces=NS) == 'Tests & more'
    assert c.mailbox(action='flags', message_ref='ews:' + NATIVE, flags=[r'\Seen', r'\Flagged'], remove_flags=True)['ok']
    update = s.operations[-1]
    assert update.get('SuppressReadReceipts') == 'true' and update.get('MessageDisposition') == 'SaveOnly'
    assert update.get('ConflictResolution') == 'NeverOverwrite' and update.find('.//t:ItemId', NS).get('ChangeKey') == 'key'
    assert update.findtext('.//t:IsRead', namespaces=NS) == 'false'
    assert update.findtext('.//t:FlagStatus', namespaces=NS) == 'NotFlagged'
    with pytest.raises(ValueError, match='support Seen'):
        c.mailbox(action='flags', message_ref='ews:' + NATIVE, flags=[r'\Seen', 'unknown'])
    assert names(s) == ['CreateFolder', 'GetItem', 'GetFolder', 'UpdateItem']


def test_incomplete_pagination_or_non_ews_ref_never_claim_success():
    c, s = client(listing('', last=False))
    with pytest.raises(EwsError, match='InvalidPageBoundary'):
        c.search()
    with pytest.raises(ValueError, match='message_ref'):
        c.read(message_ref='gmail:12')
    with pytest.raises(ValueError, match='continuation'):
        c.search(page_token='wrong token')
    assert names(s) == ['FindItem']

def test_delegated_login_cannot_reuse_another_mailbox_folder_handle():
    # Both mailboxes accept the same principal; a server would honor either raw folder ID.
    def directory(native):
        return response('FindFolder', '<m:RootFolder IncludesLastItemInRange="true"><t:Folders>'
                        f'<t:Folder><t:FolderId Id="{native}"/><t:DisplayName>Team</t:DisplayName>'
                        '</t:Folder></t:Folders></m:RootFolder>')
    a, _ = client(directory('A+/='))
    s = Session(directory('B+/='), *proof('Folder', 'B+/='), listing(item()), *proof('Folder', 'B+/='), *proof(), response('CopyItem'))
    b = EwsClient({**ACCOUNT, 'id': 'ews:other', 'address': 'other@example.invalid'}, session=s)
    foreign = a.mailbox(action='list')['folders'][0]['id']
    own = b.mailbox(action='list')['folders'][0]['id']
    for operation in (lambda: b.search(folder=foreign), lambda: b.mailbox(action='copy', message_ref='ews:' + NATIVE, destination=foreign)):
        with pytest.raises(ValueError, match='different account'):
            operation()
    assert names(s) == ['FindFolder']
    b.search(folder=own)
    b.mailbox(action='copy', message_ref='ews:' + NATIVE, destination=own)
    assert s.operations[3].find('m:ParentFolderIds/t:FolderId', NS).get('Id') == 'B+/='
    assert s.operations[-1].find('m:ToFolderId/t:FolderId', NS).get('Id') == 'B+/='
