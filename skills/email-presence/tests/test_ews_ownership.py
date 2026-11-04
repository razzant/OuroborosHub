"""A common delegate login can open both stores; only explicit mailbox access proves which one."""
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from lib.accounts import AccountStore
from lib.ews import EwsClient
from lib.ews_wire import EwsError, NS
from lib.service import MailService, register_tools
from test_ews_mailbox import ACCOUNT, identity, item, listing, names
from test_ews_wire import CONFIG, Session, response


class SharedStores(Session):
    """The server routes an implicit ItemId/FolderId independently of the claimed Mail account."""
    def __init__(self, keys=True, denied=(), folders=None):
        super().__init__()
        self.keys, self.denied = keys, set(denied)
        self.folders = folders or ['B-inbox', 'B-projects']

    def post(self, *args, **kwargs):
        op = ET.fromstring(kwargs['data']).find('s:Body', NS)[0]
        verb = op.tag.split('}')[-1]
        if verb in ('GetFolder', 'GetItem'):
            kind = verb[3:]
            ref = op.find('m:' + kind + 'Ids', NS)[0]
            native = ref.get('Id')
            if ref.tag.endswith('DistinguishedFolderId'):
                address = ref.findtext('t:Mailbox/t:EmailAddress', namespaces=NS)
                assert address in ('a@example.invalid', 'b@example.invalid')
                native = address[0].upper() + '-' + native
            if native in self.denied:
                xml = response(verb, klass='Error', code='ErrorAccessDenied')
            elif kind == 'Folder' and op.find('.//t:FieldURI[@FieldURI="folder:DisplayName"]', NS) is not None:
                xml = response('GetFolder', f'<m:Folders><t:Folder><t:FolderId Id="{native}"/>'
                               '<t:DisplayName>Projects</t:DisplayName></t:Folder></m:Folders>')
            elif op.find('.//t:IncludeMimeContent', NS) is not None:
                xml = response(verb, '<m:Items>' + item(native, mime=True) + '</m:Items>')
            else:
                assert op.find('.//t:FieldURI[@FieldURI="' + kind.lower() + ':ParentFolderId"]', NS) is not None
                assert op.find('.//t:ExtendedFieldURI[@PropertyTag="0x0FFA"]', NS) is not None
                owner = native[0]
                key = base64.b64encode(('STORE-' + owner).encode()).decode() if self.keys else None
                parent = owner + ('-projects' if kind == 'Item' else '-root')
                xml = identity(kind, native, parent=parent, key=key)
        elif verb == 'FindFolder':
            root = op.find('m:ParentFolderIds/t:DistinguishedFolderId', NS)
            assert root.get('Id') == 'msgfolderroot' and root.findtext('t:Mailbox/t:EmailAddress', namespaces=NS) == 'b@example.invalid'
            if 'B-msgfolderroot' in self.denied:
                xml = response(verb, klass='Error', code='ErrorAccessDenied')
            else:
                offset = int(op.find('m:IndexedPageFolderView', NS).get('Offset'))
                terminal = offset + 1 == len(self.folders)
                xml = response(verb, '<m:RootFolder IncludesLastItemInRange="' + str(terminal).lower() +
                               '" IndexedPagingOffset="' + str(offset + 1) + '"><t:Folders><t:Folder><t:FolderId Id="' +
                               self.folders[offset] + '"/></t:Folder></t:Folders></m:RootFolder>')
        elif verb == 'FindItem':
            parent = op.find('m:ParentFolderIds', NS)[0]
            owner = parent.get('Id')[0] if parent.tag.endswith('}FolderId') else 'B'
            xml = listing(item(owner + '-msg'))
        elif verb in ('CopyItem', 'MoveItem', 'UpdateItem'):
            xml = response(verb)
        elif verb == 'CreateItem':
            xml = response(verb, '<m:Items><t:Message><t:ItemId Id="B-saved"/></t:Message></m:Items>')
        elif verb == 'CreateFolder':
            xml = response(verb, '<m:Folders><t:Folder><t:FolderId Id="B-new"/></t:Folder></m:Folders>')
        else:
            raise AssertionError(verb)
        self.responses.append(xml)
        return super().post(*args, **kwargs)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    registry = AccountStore(tmp_path)
    def create(letter):
        return registry.save_ews({'address': letter + '@example.invalid', 'endpoint': CONFIG['EWS_ENDPOINT'],
                                  'username': 'one-delegate', 'password': 'synthetic'})
    a, b = create('a'), create('b')
    provider = SharedStores()
    monkeypatch.setattr('lib.service.account_client', lambda _, account, **kw: EwsClient(account, session=provider))
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    handlers = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **_: handlers.update({name: handler})), service)
    def call(tool, **args): return json.loads(handlers['email_' + tool](account_id=b['id'], **args))
    return a, b, provider, call


def legacy_message(account, native):
    # Exact 0.10.0 format, including a hand-edited wrapper claiming B for A's original ID.
    raw = json.dumps({'account_id': account['id'], 'ref': 'ews:' + native}, separators=(',', ':')).encode()
    return 'mail:' + base64.urlsafe_b64encode(raw).decode().rstrip('=')


@pytest.mark.parametrize('tool,args,foreign_field,effect', [
    ('mark_read', {}, 'item', 'UpdateItem'), ('mailbox', {'action':'trash'}, 'item', 'MoveItem'),
    ('mailbox', {'action':'move'}, 'item', 'MoveItem'), ('mailbox', {'action':'copy'}, 'folder', 'CopyItem'),
    ('search', {}, 'folder', 'FindItem'), ('draft', {'to':'p@example.invalid','subject':'draft'}, 'folder', 'CreateItem'),
    ('mailbox', {'action':'create','destination':'new'}, 'folder', 'CreateFolder'), ('read', {}, 'item', 'GetItem'),
])
def test_legacy_handles_checked_before_read_or_effect(tools, tool, args, foreign_field, effect):
    _, b, provider, call = tools
    def invoke(foreign):
        owner = 'A' if foreign else 'B'
        a = dict(args)
        if foreign_field == 'item' or args.get('action') == 'copy':
            a['message_ref'] = legacy_message(b, (owner if foreign_field == 'item' else 'B') + '-msg')
        if foreign_field == 'folder' or args.get('action') == 'move':
            dest = f'ews-folder:{b["id"]}:{owner if foreign_field == "folder" else "B"}-projects'
            a['destination' if args.get('action') in ('move','copy') else 'folder'] = dest
        return call(tool, **a)
    with pytest.raises(EwsError, match='MailboxOwnershipMismatch'):
        invoke(True)
    assert set(names(provider)) <= {'GetItem','GetFolder'}
    assert all(op.find('.//t:IncludeMimeContent', NS) is None for op in provider.operations)
    provider.operations.clear()
    result = invoke(False)
    if tool == 'draft':
        assert result['status'] == 'needs_arguments' and effect not in names(provider)
        retry = dict(result['retry_arguments'])
        assert retry.pop('account_id') == b['id'] and retry['folder_names'] == ['Projects']
        result = call(tool, **retry)
    assert result and effect in names(provider)
    if tool == 'read':
        assert provider.operations[-1].find('.//t:IncludeMimeContent', NS) is not None


def test_absent_store_property_uses_actual_paged_mailbox_hierarchy(tools):
    _, b, provider, call = tools
    provider.keys = False
    assert call('read', message_ref=legacy_message(b,'B-msg'))['body'] == 'Письмо.'
    assert names(provider).count('FindFolder') == 2
    provider.operations.clear()
    with pytest.raises(EwsError, match='MailboxOwnershipUnverified'):
        call('mark_read', message_ref=legacy_message(b,'A-msg'))
    assert 'UpdateItem' not in names(provider)


def test_delegated_drafts_only_does_not_require_inbox_or_folder_tree(tools):
    _, b, provider, call = tools
    provider.denied = {'B-inbox', 'B-msgfolderroot'}
    assert call('mark_read', message_ref=legacy_message(b,'B-msg'))['ok']
    assert names(provider)[-1] == 'UpdateItem'
    provider.operations.clear()
    with pytest.raises(EwsError, match='MailboxOwnershipMismatch'):
        call('mark_read', message_ref=legacy_message(b,'A-msg'))
    assert 'UpdateItem' not in names(provider)


@pytest.mark.parametrize('owner', ['A', 'B'])
def test_projection_refused_does_not_trust_the_handle_or_block_verified_parent(owner):
    account = {**ACCOUNT, 'address': 'b@example.invalid'}
    refused = response('GetItem', klass='Error', code='ErrorInvalidPropertyRequest')
    replies = [refused, identity('Item', owner+'-msg', parent=owner+'-inbox', key=None),
               identity('Folder','B-inbox')]
    if owner == 'A':
        replies += [response('FindFolder','<m:RootFolder IncludesLastItemInRange="true"><t:Folders/></m:RootFolder>')]
        # Account has only its Inbox. No other explicit distinguished anchor is accessible.
        replies += [response('GetFolder', klass='Error', code='ErrorAccessDenied')] * 7
    else:
        replies += [response('UpdateItem')]
    s = Session(*replies)
    c = EwsClient(account, session=s)
    if owner == 'A':
        with pytest.raises(EwsError, match='MailboxOwnershipUnverified'):
            c.mark_read(message_ref='ews:A-msg')
        assert 'UpdateItem' not in names(s)
    else:
        assert c.mark_read(message_ref='ews:B-msg')['ok']
    assert s.operations[0].find('.//t:ExtendedFieldURI', NS) is not None
    assert s.operations[1].find('.//t:ExtendedFieldURI', NS) is None


def test_relabelled_target_allowed_by_host_still_cannot_touch_paused_exchange(tools, tmp_path, monkeypatch):
    import yaml
    source = os.environ.get('OUROBOROS_SOURCE_DIR')
    if not source:
        pytest.skip('Set OUROBOROS_SOURCE_DIR to exercise the real Place guard')
    monkeypatch.syspath_prepend(str(Path(source).resolve()))
    from ouroboros import extension_registry_state as registry, place_guard, places
    from ouroboros.extension_surface_names import extension_surface_name
    a, b, provider, call = tools
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / 'SKILL.md').read_text(encoding='utf-8').split('---',2)[1])
    descriptor = {'skill':'email-presence', 'extension_generation':'synthetic'}
    bundle = registry._ExtensionRegistrations(generation_digest='synthetic',
                        loaded_skill=SimpleNamespace(manifest=SimpleNamespace(raw_extra=manifest)))
    full = extension_surface_name('email-presence','email_mark_read')
    monkeypatch.setitem(registry._extensions,'email-presence',bundle)
    monkeypatch.setitem(registry._tools,full,descriptor)
    drive = tmp_path / 'host'
    places.connect_place(drive, connector='email-presence', provider='email', account_id=a['id'], state='paused')
    ctx = SimpleNamespace(drive_root=drive, task_id='ordinary', task_metadata={})
    def guard(account):
        return place_guard.dispatch_refusal(ctx,full,{'account_id':account['id'],
            'message_ref':legacy_message(account,'A-msg')},ext_tool=descriptor)
    assert 'Paused' in guard(a) and not guard(b)
    with pytest.raises(EwsError, match='MailboxOwnershipMismatch'):
        call('mark_read', message_ref=legacy_message(b,'A-msg'))
    assert names(provider) == ['GetItem','GetFolder']
    assert call('mark_read', message_ref=legacy_message(b,'B-msg'))['ok']
    assert names(provider)[-1] == 'UpdateItem'
