"""An unreadable MIME rendering must not lose the native message or the mail following it."""
import json
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
import requests

from lib.accounts import AccountStore
from lib.ews import EwsClient
from lib.ews_wire import EwsError, NS
from lib.service import MailService, register_tools
from lib.store import EmailStore
from test_ews_mailbox import ACCOUNT, item
from test_ews_wire import CONFIG, Session, response

NATIVE = 'B+/=Case'
FOLDER = 'inbox-id'
CURSOR = 'ews_sync:' + FOLDER
FAILED_MIME = response('GetItem', klass='Error', code='ErrorMimeContentConversionFailed')


def properties(native):
    return f'''<t:Message><t:ItemId Id="{native}"/><t:ParentFolderId Id="{FOLDER}"/><t:Subject>Проверка</t:Subject>
        <t:Body BodyType="Text">Содержимое Exchange.</t:Body><t:DateTimeReceived>2026-11-04T00:00:01Z</t:DateTimeReceived>
        <t:From><t:Mailbox><t:EmailAddress>/o=example/ou=users/cn=U</t:EmailAddress><t:RoutingType>EX</t:RoutingType></t:Mailbox></t:From>
        <t:ToRecipients><t:Mailbox><t:EmailAddress>owner@example.invalid</t:EmailAddress></t:Mailbox>
          <t:Mailbox><t:EmailAddress>/o=example/ou=users/cn=team</t:EmailAddress><t:RoutingType>EX</t:RoutingType></t:Mailbox></t:ToRecipients>
        <t:ReplyTo><t:Mailbox><t:EmailAddress>replies@example.invalid</t:EmailAddress></t:Mailbox></t:ReplyTo>
        <t:InternetMessageHeaders>
          <t:InternetMessageHeader HeaderName="From">Автор &lt;sender@example.invalid&gt;</t:InternetMessageHeader>
          <t:InternetMessageHeader HeaderName="To">owner@example.invalid, team@example.invalid</t:InternetMessageHeader>
          <t:InternetMessageHeader HeaderName="Cc">Colleague &lt;other@example.invalid&gt;</t:InternetMessageHeader>
          <t:InternetMessageHeader HeaderName="List-Id">mail.example.invalid</t:InternetMessageHeader>
          <t:InternetMessageHeader HeaderName="Message-ID">&lt;original@example.invalid&gt;</t:InternetMessageHeader>
          <t:InternetMessageHeader HeaderName="References">&lt;root@example.invalid&gt; &lt;parent@example.invalid&gt;</t:InternetMessageHeader>
          <t:InternetMessageHeader HeaderName="In-Reply-To">&lt;parent@example.invalid&gt;</t:InternetMessageHeader>
        </t:InternetMessageHeaders>
        <t:Attachments><t:FileAttachment><t:AttachmentId Id="att+/="/><t:Name>отчёт.pdf</t:Name>
          <t:ContentType>application/pdf</t:ContentType><t:Size>234</t:Size></t:FileAttachment></t:Attachments>
        <t:IsRead>false</t:IsRead><t:Flag><t:FlagStatus>Flagged</t:FlagStatus></t:Flag></t:Message>'''


class Mailbox(Session):
    def __init__(self, *, fail_properties=False):
        super().__init__()
        self.fail_properties = fail_properties
        self.calls = []

    def post(self, *args, **kwargs):
        op = ET.fromstring(kwargs['data']).find('s:Body', NS)[0]
        name = op.tag.split('}')[-1]
        if name == 'GetFolder':
            answer = response(name, f'<m:Folders><t:Folder><t:FolderId Id="{FOLDER}"/></t:Folder></m:Folders>')
        elif name == 'SyncFolderItems':
            assert op.findtext('m:SyncState', namespaces=NS) == 'before'
            ids = ['normal-second', NATIVE, 'normal-first']
            creates = ''.join(f'<t:Create><t:Message><t:ItemId Id="{i}"/></t:Message></t:Create>' for i in ids)
            answer = response(name, '<m:SyncState>after</m:SyncState><m:IncludesLastItemInRange>true</m:IncludesLastItemInRange>'
                              + f'<m:Changes>{creates}</m:Changes>')
        else:
            assert name == 'GetItem'
            native = op.find('m:ItemIds/t:ItemId', NS).get('Id')
            mime = op.find('m:ItemShape/t:IncludeMimeContent', NS) is not None
            self.calls.append((native, mime))
            if native != NATIVE:
                assert mime
                answer = response(name, '<m:Items>' + item(native, mime=True, mid=False) + '</m:Items>')
            elif mime:
                answer = FAILED_MIME
            else:
                fields = {x.get('FieldURI') for x in op.findall('m:ItemShape/t:AdditionalProperties/t:FieldURI', NS)}
                assert {'item:Body', 'item:Attachments', 'item:InternetMessageHeaders', 'message:InternetMessageId',
                        'message:References', 'message:ReplyTo', 'message:From', 'message:ToRecipients', 'message:CcRecipients'} <= fields
                assert op.findtext('m:ItemShape/t:BodyType', namespaces=NS) == 'Text'
                answer = (requests.Timeout('fixture') if self.fail_properties else
                          response(name, '<m:Items>' + properties(native) + '</m:Items>'))
        self.responses.append(answer)
        return super().post(*args, **kwargs)


def test_public_read_failed_mime_returns_native_fields_and_no_fabricated_file(tmp_path, monkeypatch):
    accounts = AccountStore(tmp_path)
    account = accounts.save_ews({'address': ACCOUNT['address'], 'password': 'fixture', 'endpoint': CONFIG['EWS_ENDPOINT']})
    provider = Mailbox()
    monkeypatch.setattr('lib.service.account_client', lambda registry, account, **kw: EwsClient(account, session=provider))
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    handlers = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **_: handlers.update({name: handler})), service)
    reference = service.wrap_ref(account, {'message_ref': 'ews:' + NATIVE})
    result = json.loads(handlers['email_read'](account_id=account['id'], message_ref=reference))
    assert result['message_ref'] == reference and result['provider_id'] == NATIVE
    assert result['subject'] == 'Проверка' and result['body'] == 'Содержимое Exchange.' and result['flags'] == [r'\Flagged']
    assert result['sender'] == 'sender@example.invalid' and result['sender_name'] == 'Автор'
    assert (result['recipients'], result['cc'], result['reply_to']) == (
        ['owner@example.invalid', 'team@example.invalid'], ['other@example.invalid'], ['replies@example.invalid'])
    assert (result['message_id'], result['conversation_id'], result['in_reply_to']) == (
        '<original@example.invalid>', '<root@example.invalid>', '<parent@example.invalid>')
    assert result['headers']['list-id'] == ['mail.example.invalid']
    assert result['source_format'] == 'ews_properties' and 'MIME' in result['source_note']
    assert result['attachments'] == [{'file_name': 'отчёт.pdf', 'mime_type': 'application/pdf', 'size': 234,
                                      'content_available': False, 'provider_attachment_id': 'att+/='}]
    assert 'source_artifact' not in result and not result.get('staged_files') and not list(tmp_path.rglob('staged'))
    assert provider.calls == [(NATIVE, True), (NATIVE, False)]


def _active(store):
    store.put(CURSOR, {'state': 'before', 'initializing': False, 'activated_at': 0})


def test_sync_reads_past_failed_mime_by_retaining_native_message(tmp_path):
    store, session = EmailStore(tmp_path), Mailbox()
    _active(store)
    ews = EwsClient(ACCOUNT, session=session)
    for _ in range(5):
        ews.poll(store)
        if store.get(CURSOR)['state'] == 'after':
            break
    assert store.get(CURSOR)['state'] == 'after' and store.status()['inbox'] == {'pending': 3}
    with store._connect() as db:
        rows = {ctx['provider_id']: (r['body'], ctx, r['staged_files_json']) for r in db.execute('SELECT * FROM inbox')
                for ctx in [json.loads(r['context_json'])]}
    body, info, staged = rows[NATIVE]
    assert body == 'Содержимое Exchange.' and info['source_format'] == 'ews_properties' and 'MIME' in info['source_note']
    assert info['provider_event_key'] == 'ews:' + NATIVE and info['attachments'][0]['content_available'] is False
    assert info['attachments'][0]['size'] == 234 and 'source_artifact' not in info and json.loads(staged) == []
    for normal in ['normal-first', 'normal-second']:
        assert rows[normal][1]['source_artifact']['size'] > 0 and (normal, False) not in session.calls


def test_alternate_read_timeout_preserves_owed_sync_page(tmp_path):
    store, session = EmailStore(tmp_path), Mailbox(fail_properties=True)
    _active(store)
    ews = EwsClient(ACCOUNT, session=session)
    with pytest.raises(EwsError, match='TransportFailure'):
        for _ in range(5):
            ews.poll(store)
    assert store.get(CURSOR)['state'] == 'before' and store.message_by_id('<original@example.invalid>') is None
    session.fail_properties = False
    for _ in range(5):
        ews.poll(store)
        if store.get(CURSOR)['state'] == 'after':
            break
    assert store.get(CURSOR)['state'] == 'after' and store.status()['inbox'] == {'pending': 3}


@pytest.mark.parametrize(('klass', 'code'), [('Error', 'ErrorServerBusy'), ('Warning', 'ErrorMimeContentConversionFailed')])
def test_only_definitive_item_local_conversion_failure_uses_other_shape(klass, code):
    session = Session(response('GetItem', klass=klass, code=code))
    with pytest.raises(EwsError):
        EwsClient(ACCOUNT, session=session).read(message_ref='ews:' + NATIVE)
    assert len(session.operations) == 1


def test_alternate_get_item_must_still_match_requested_opaque_id():
    session = Session(FAILED_MIME, response('GetItem', '<m:Items>' + properties('other') + '</m:Items>'))
    with pytest.raises(EwsError, match='ItemIdentityMismatch'):
        EwsClient(ACCOUNT, session=session).read(message_ref='ews:' + NATIVE)
