"""Exercise real Host target extraction and the provider requests made after it."""
from contextlib import contextmanager
import json
import os
from types import SimpleNamespace

import pytest

from lib.client import MailClient
from lib.gmail import GmailClient
from lib.service import MailService, register_tools
from lib.store import EmailStore
from test_declared_target_preparation import MANIFEST, published_targets

pytest_plugins = ["test_binding_core"]


def host_guard(tmp_path, monkeypatch, tool, account, scopes, *, drive=None):
    source = os.environ.get('OUROBOROS_SOURCE_DIR')
    if not source:
        pytest.skip('Set OUROBOROS_SOURCE_DIR for the real Host guard')
    monkeypatch.syspath_prepend(source)
    from ouroboros import extension_registry_state as registry, place_guard, places
    from ouroboros.extension_surface_names import extension_surface_name
    drive = drive or tmp_path / 'host'
    name = extension_surface_name('email-presence', tool)
    ext = {'skill': 'email-presence', 'extension_generation': 'mail-effect'}
    bundle = registry._ExtensionRegistrations(generation_digest='mail-effect',
        loaded_skill=SimpleNamespace(manifest=SimpleNamespace(raw_extra=MANIFEST)))
    monkeypatch.setitem(registry._extensions, 'email-presence', bundle)
    monkeypatch.setitem(registry._tools, name, ext)
    for conversation, state in scopes:
        places.connect_place(drive, connector='email-presence', provider='email', account_id=account,
                             conversation_id=conversation, state=state)
    ctx = SimpleNamespace(drive_root=drive, task_id='fixture', task_metadata={})
    return lambda args: place_guard.dispatch_refusal(ctx, name, args, ext_tool=ext)


def tools_for(tmp_path, provider, client, *, account_id=None):
    account = {'id': account_id or provider + ':fixture', 'address': 'owner@example.invalid', 'provider': provider,
               'senders': ['owner@example.invalid']}
    @contextmanager
    def target(account_id):
        assert account_id == account['id']
        yield account, EmailStore(tmp_path / 'email'), client
    svc = MailService.__new__(MailService)
    svc.target = target
    handlers = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **_: handlers.update({name: handler})), svc)
    return account['id'], lambda tool, args: json.loads(handlers[tool](**args))


def gmail_for(tmp_path, *, label_rows=None, reads=None):
    client = GmailClient.__new__(GmailClient)
    writes = []
    def request(method, path, **options):
        if method == 'GET':
            assert path == '/labels'
            if reads is not None:
                reads.append(path)
            return {'labels': label_rows if label_rows is not None else [
                {'id':'Label_1', 'name':'Public'}, {'id':'Label_2', 'name':'Archive Team'}]}
        writes.append((method, path, options.get('body')))
        return {}
    client._request = request
    return (*tools_for(tmp_path, 'gmail', client), writes)


@pytest.mark.parametrize(('args','held','expected'), [
    ({'action':'labels','add_labels':['Label_1','Label_2']}, 'Label_2', {'addLabelIds':['Label_1','Label_2'], 'removeLabelIds':[]}),
    ({'action':'labels','remove_labels':['Label_2']}, 'Label_2', {'addLabelIds':[], 'removeLabelIds':['Label_2']}),
    ({'action':'copy','destination':'Public','remove_labels':['Label_2']}, 'Label_2', {'addLabelIds':['Label_1'], 'removeLabelIds':['Label_2']}),
    ({'action':'move','destination':'Public','folder':'Archive Team'}, 'Label_2', {'addLabelIds':['Label_1'], 'removeLabelIds':['Label_2']}),
    ({'action':'move','destination':'Public'}, 'INBOX', {'addLabelIds':['Label_1'], 'removeLabelIds':['INBOX']}),
    ({'action':'flags','flags':['\\Seen','\\Flagged']}, 'UNREAD', {'addLabelIds':['STARRED'], 'removeLabelIds':['UNREAD']}),
    ({'action':'archive'}, 'INBOX', {'addLabelIds':[], 'removeLabelIds':['INBOX']}),
    ({'action':'update_label','destination':'Archive Team','properties':{'name':'New label'}}, 'Label_2', {'name':'New label'}),
    ({'action':'delete_label','label_id':'Archive Team'}, 'Label_2', None),
])
@pytest.mark.parametrize('restricted',[False,True])
def test_host_checks_all_gmail_labels_before_the_actual_write(tmp_path, monkeypatch, args, held, expected, restricted):
    account, call, writes = gmail_for(tmp_path)
    tool = 'email_mailbox'
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active'), (held, 'silent' if restricted else 'active')])
    args = {**args, 'account_id':account, 'uid':123,'uidvalidity':1}
    for _ in range(2):
        refusal = guard(args)
        if refusal:
            assert restricted and 'Silent' in refusal and not writes
            return
        response = call(tool, args)
        if response.get('status') != 'needs_arguments':
            break
        assert not writes
        args = response['retry_arguments']
    assert not restricted and len(writes) == 1
    assert writes[0][2] == expected
    if args['action'] in ('update_label','delete_label'):
        assert writes[0][1] == '/labels/Label_2'
    else:
        assert writes[0][:2] == ('POST','/messages/7b/modify')
    visible = {v for vals in published_targets(tool, args).values() for v in vals}
    assert held in visible
    if expected and 'addLabelIds' in expected:
        assert set(expected['addLabelIds'] + expected['removeLabelIds']) <= visible


@pytest.mark.parametrize(('provider','folder_arg','expected'), [
    ('imap',{},'Drafts'),('imap',{'folder':''},'Drafts'),('imap',{'folder':'SharedArchive'},'SharedArchive'),('imap',{'folder':'Inbox'},'INBOX'),
    ('gmail',{},'DRAFT'),('gmail',{'folder':'Drafts'},'DRAFT')])
def test_draft_declares_selected_folder_before_staging_or_writing(tmp_path, provider, folder_arg, expected):
    writes = []
    class Capture:
        def draft(self, **kwargs):
            writes.append(kwargs['folder'])
            return {'ok':True}
    client = Capture()
    if provider == 'imap':
        client = MailClient({'EMAIL_USER':'owner@example.invalid'})
        class Box:
            def append(self, folder, *rest):
                writes.append(folder); return 'OK',[b'saved']
        @contextmanager
        def imap(*args,**kw): yield Box()
        client.imap = imap
    account, call = tools_for(tmp_path, provider, client)
    args = {'account_id':account, 'subject':'Draft', 'to':'reader@example.invalid', **folder_arg}
    result = call('email_draft',args)
    if folder_arg.get('folder') != expected:
        assert result['status'] == 'needs_arguments' and not writes
        args = result['retry_arguments']; result = call('email_draft',args)
    assert result['ok']
    assert published_targets('email_draft',args)['folder'] == [expected]
    assert writes == [client.folder_name(expected) if provider=='imap' else expected]


@pytest.mark.parametrize('scene,held', [
    ('unbound',False),('account_active',False),('account_paused',True),
    ('folder_paused',True),('other_folder',False),('global_folder',True),('rfc_silent',True)])
def test_draft_uses_the_real_host_for_both_rfc_and_folder(tmp_path, monkeypatch, core_host, scene, held):
    places, drive, adapter = core_host
    writes = []
    class Capture:
        def draft(self, **kwargs): writes.append(kwargs['folder']); return {'ok':True}
    account, call = tools_for(tmp_path, 'imap', Capture())
    # Bind the service to this fixture's actual HTTP endpoint, not a live host.
    @contextmanager
    def host(_self):
        yield adapter
    monkeypatch.setattr(MailService, '_presence_host', host)
    scope = []
    if scene in ('account_active','account_paused','folder_paused'):
        scope.append(('', 'paused' if scene=='account_paused' else 'active'))
    if scene in ('folder_paused','rfc_silent'):
        scope.append(('SharedArchive' if scene=='folder_paused' else '<root>','paused' if scene=='folder_paused' else 'silent'))
    if scene in ('other_folder','global_folder'):
        scope.extend([('<root>','active'), ('Unrelated','silent')])
    for room,state in scope:
        places.connect_place(drive, connector='email-presence',provider='email',account_id=account,conversation_id=room,state=state)
    if scene=='global_folder':
        places.connect_place(drive,connector='email-presence',provider='email',account_id='',conversation_id='SharedArchive',state='silent')
    guard = host_guard(tmp_path, monkeypatch, 'email_draft', account, [], drive=drive)
    args={'account_id':account,'folder':'SharedArchive','subject':'Reply','to':'reader@example.invalid',
          'references':['<root>'],'conversation_id':'<root>','thread_id':'<root>'}
    refusal=guard(args)
    if refusal:
        assert held and scene in ('account_paused','rfc_silent') and not writes
    else:
        result=call('email_draft',args)
        assert result['ok'] is not held and writes == ([] if held else ['SharedArchive'])
        if held: assert result['code']=='draft_folder_held'


@pytest.mark.parametrize("failure", ["lookup", "bind", "offline"])
def test_draft_does_not_treat_an_outage_as_no_place(failure):
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter
    def host(req):
        if failure == 'offline':
            raise httpx.ConnectError("unreachable", request=req)
        if req.url.path=='/identity':
            return httpx.Response(200,json={'places_version':1,'presence_place_binding_version':1,'presence_delivery_version':1})
        if req.url.params.get('operation')=='bind' and failure != 'bind':
            return httpx.Response(200,json={'allowed':False,'code':'place_unattributable','binding':None})
        return httpx.Response(503,json={'code':'places_unavailable'})
    with httpx.Client(transport=httpx.MockTransport(host)) as http:
        adapter=LoopbackPresenceHostAdapter('',host_service_url='http://127.0.0.1:8767',skill_token='fixture',place_http_client=http,sync_only=True)
        assert 'unavailable' in adapter.draft_folder_hold('imap:x','Shared')


@pytest.mark.parametrize(('places_version','payload','held'), [
    (0, None, False),
    (1, {'place_id':'folder','state':'paused','label':'Shared'}, True),
    (1, {'code':'place_not_connected'}, False),
    (1, {'detail':'Not Found'}, True),
])
def test_older_host_keeps_unbound_drafts_but_not_unknown_state(places_version,payload,held):
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter
    seen = []
    def host(req):
        seen.append(req.url.path)
        if req.url.path=='/identity':
            return httpx.Response(200,json={'places_version':places_version,'presence_delivery_version':1})
        assert req.url.path=='/presence/place' and 'operation' not in req.url.params
        return httpx.Response(200 if payload.get('state') else 404,json=payload)
    with httpx.Client(transport=httpx.MockTransport(host)) as http:
        adapter=LoopbackPresenceHostAdapter('',host_service_url='http://127.0.0.1:8767',skill_token='fixture',place_http_client=http,sync_only=True)
        assert bool(adapter.draft_folder_hold('imap:x','Shared')) == held
    assert seen == ['/identity'] + (['/presence/place'] if places_version else [])


@pytest.mark.parametrize('selected,restricted', [
    ('SharedArchive', 'id'), ('id', 'SharedArchive'), ('id', 'id'),
    ('SharedArchive', ''), ('drafts', ''), ('id', 'drafts')])
def test_exchange_save_uses_same_resolved_folder_and_cannot_hide_its_other_name(tmp_path, monkeypatch, core_host, selected, restricted):
    from lib.ews_wire import NS
    from test_ews_mailbox import client, proof, names, response
    places,drive,adapter=core_host
    native='F1'
    stable='ews-folder:ews:mailbox:F1'
    meta=response('GetFolder','<m:Folders><t:Folder><t:FolderId Id="F1"/><t:DisplayName>SharedArchive</t:DisplayName>'
                  '<t:DistinguishedFolderId>drafts</t:DistinguishedFolderId></t:Folder></m:Folders>')
    listed=response('FindFolder','<m:RootFolder IncludesLastItemInRange="true"><t:Folders><t:Folder><t:FolderId Id="F1"/>'
                    '<t:DisplayName>SharedArchive</t:DisplayName></t:Folder></t:Folders></m:RootFolder>')
    created=response('CreateItem','<m:Items><t:Message><t:ItemId Id="New"/></t:Message></m:Items>')
    initial=proof('Folder',native) if selected=='id' else ((listed,) if selected=='SharedArchive' else ())
    exchange,session = client(*initial,meta,*proof('Folder', native),meta,created)
    account,call = tools_for(tmp_path,'ews',exchange,account_id=exchange.account['id'])
    @contextmanager
    def host(_self): yield adapter
    monkeypatch.setattr(MailService,'_presence_host',host)
    places.connect_place(drive,connector='email-presence',provider='email',account_id=account,state='active')
    if restricted:
        places.connect_place(drive,connector='email-presence',provider='email',account_id=account,
                             conversation_id=stable if restricted=='id' else restricted,state='silent')
    args={'account_id':account,'folder':stable if selected=='id' else selected,'to':'reader@example.invalid','subject':'Draft',
          'references':['<root>'],'conversation_id':'<root>','thread_id':'<root>'}
    prepared=call('email_draft',args)
    assert prepared['status']=='needs_arguments' and 'CreateItem' not in names(session)
    args=prepared['retry_arguments']
    assert args['folder']==stable and set(args['folder_names'])=={'SharedArchive','draft','drafts'}
    facts=published_targets('email_draft',args)
    assert facts['folder']==[stable] and facts['folder_names']==args['folder_names']
    result=call('email_draft',args)
    if restricted:
        assert result['code']=='draft_folder_held' and 'CreateItem' not in names(session)
    else:
        assert result['ok'] and names(session)[-1]=='CreateItem'
        assert session.operations[-1].find('m:SavedItemFolderId/t:FolderId',NS).get('Id')==native
        assert names(session).count('FindFolder')==(1 if selected=='SharedArchive' else 0)


@pytest.mark.parametrize(('action','selection'), [
    ('copy',{'destination':'Label_2'}), ('move',{'folder':'Label_2','destination':'INBOX'}),
    ('labels',{'add_labels':['Label_2']}), ('labels',{'remove_labels':['Label_2']}),
    ('update_label',{'label_id':'Label_2','properties':{'name':'New','labelListVisibility':'labelShow'}}),
    ('delete_label',{'destination':'Label_2'}), ('trash',{}), ('untrash',{})])
@pytest.mark.parametrize('restricted', [False,True])
def test_gmail_ids_respect_current_label_name_and_native_trash(tmp_path, monkeypatch, action, selection, restricted):
    account,call,writes = gmail_for(tmp_path)
    guarded = 'TRASH' if action in ('trash','untrash') else 'Archive Team'
    guard = host_guard(tmp_path,monkeypatch,'email_mailbox',account,[('', 'active'),(guarded,'paused' if restricted else 'active')])
    args = {'account_id':account,'action':action,'uid':123,'uidvalidity':1,**selection}
    assert not guard(args)
    prepared = call('email_mailbox',args)
    assert prepared['status']=='needs_arguments' and not writes
    retry = prepared['retry_arguments']
    assert guarded in published_targets('email_mailbox',retry)['label_names']
    if restricted:
        assert 'Paused' in guard(retry) and not writes
    else:
        assert not guard(retry)
        call('email_mailbox',retry)
        assert len(writes)==1
        if action in ('trash','untrash'):
            assert writes[0] == ('POST','/messages/7b/'+action,{})
        if action == 'update_label':
            assert writes[0] == ('PATCH','/labels/Label_2',selection['properties'])


def test_move_keeps_stable_ids_on_name_change_and_lists_labels_once_per_dispatch(tmp_path):
    rows=[{'id':'Label_1','name':'From'}, {'id':'Label_2','name':'To'}, {'id':'Label_3','name':'Extra'}]
    reads=[]
    account,call,writes=gmail_for(tmp_path,label_rows=rows,reads=reads)
    args={'account_id':account,'action':'move','folder':'From','destination':'To','uid':123,'uidvalidity':1,
          'remove_labels':['Label_3'], 'label_names':['invented']}
    first=call('email_mailbox',args)
    assert reads==['/labels'] and not writes
    retry=first['retry_arguments']
    assert retry['folder']=='Label_1' and set(retry['remove_labels'])=={'Label_1','Label_3'}
    rows[0]['name']='Renamed'
    rows.append({'id':'Label_4','name':'From'})
    second=call('email_mailbox',retry)
    assert len(reads)==2 and not writes
    final=second['retry_arguments']
    assert 'Renamed' in final['label_names'] and final['folder']=='Label_1'
    call('email_mailbox',final)
    assert len(reads)==3
    assert writes==[('POST','/messages/7b/modify',{'addLabelIds':['Label_2'],'removeLabelIds':['Label_3','Label_1']})]
    with pytest.raises(ValueError,match='current Gmail label IDs'):
        call('email_mailbox',{**args,'action':'labels','add_labels':['Renamed']})
    assert len(writes)==1


@pytest.mark.parametrize('restricted',[False,True])
def test_gmail_mark_read_guards_only_its_fixed_unread_removal(tmp_path,monkeypatch,restricted):
    account,call,writes=gmail_for(tmp_path)
    guard=host_guard(tmp_path,monkeypatch,'email_mark_read',account,[('', 'active'),('UNREAD','silent' if restricted else 'active')])
    reference=MailService.wrap_ref({'id':account},{'message_ref':'gmail:7b'})
    # The caller cannot use this read-flag shortcut to remove any other label.
    args={'message_ref':reference,'account_id':account,'remove_labels':['EXTRA']}
    prepared=call('email_mark_read',args)
    assert prepared['status']=='needs_arguments' and not writes
    retry=prepared['retry_arguments']
    assert retry['message_ref']==reference and retry['remove_labels']==['UNREAD']
    if restricted:
        assert 'Silent' in guard(retry) and not writes
    else:
        assert not guard(retry)
        call('email_mark_read',retry)
        assert writes==[('POST','/messages/7b/modify',{'addLabelIds':[],'removeLabelIds':['UNREAD']})]


@pytest.mark.parametrize('restricted',[False,True])
def test_omitted_gmail_draft_folder_keeps_both_supported_spellings(tmp_path,monkeypatch,core_host,restricted):
    places,drive,adapter=core_host
    writes=[]
    class Client:
        def draft(self, **args): writes.append(args['folder']); return {'ok':True}
    account,call=tools_for(tmp_path,'gmail',Client())
    @contextmanager
    def host(_): yield adapter
    monkeypatch.setattr(MailService,'_presence_host',host)
    places.connect_place(drive,connector='email-presence',provider='email',account_id=account,state='active')
    places.connect_place(drive,connector='email-presence',provider='email',account_id=account,
                         conversation_id='Drafts',state='silent' if restricted else 'active')
    args={'account_id':account,'subject':'Draft','to':'reader@example.invalid','references':['<r>'],'conversation_id':'<r>','thread_id':'<r>'}
    prepared=call('email_draft',args)
    assert prepared['status']=='needs_arguments' and not writes
    retry=prepared['retry_arguments']
    assert retry['folder']=='DRAFT' and retry['folder_names']==['Drafts']
    result=call('email_draft',retry)
    assert bool(result['ok']) is not restricted and writes==([] if restricted else ['DRAFT'])


def test_imap_mailbox_reports_the_folder_in_its_authentic_reference_before_store(tmp_path):
    writes=[]
    imap=MailClient({'EMAIL_USER':'owner@example.invalid'})
    class Box:
        def response(self,key): return key,[b'7' if key=='UIDVALIDITY' else b'8']
        def uid(self,*command): writes.append(command);return 'OK',[b'done']
    @contextmanager
    def open_box(folder,*,readonly=False):
        writes.append((folder,readonly));yield Box()
    imap.imap=open_box
    account,call=tools_for(tmp_path,'imap',imap)
    ref=MailService.wrap_ref({'id':account},{'uid':3,'uidvalidity':7,'folder':'Private'})
    args={'account_id':account,'folder':'Other','message_ref':ref,'action':'flags','flags':[r'\Seen']}
    assert published_targets('email_mailbox',args)=={'account_id':[account]}
    result=call('email_mailbox',args)
    assert result['status']=='needs_arguments' and not writes
    retry=result['retry_arguments']
    assert retry['message_ref']==ref and published_targets('email_mailbox',retry)['folder_names']==['Private']
    assert call('email_mailbox',retry)['ok']
    assert writes==[('Private',False),('STORE','3','+FLAGS.SILENT',r'(\Seen)')]


@pytest.fixture
def sole_gmail(tmp_path,monkeypatch):
    import httpx
    import time
    from lib.accounts import AccountStore
    directory = tmp_path / 'skill'
    account = 'google:fixture'
    registry = AccountStore(directory)
    registry.save({'id':account, 'provider':'gmail', 'address':'owner@example.invalid',
        'sender':'owner@example.invalid', 'senders':['owner@example.invalid'], 'scopes':['https://mail.google.com/'],
        'tokens':{'access_token':'fixture-only','expires_at':time.time()+3600}})
    writes = []
    def exchange(request):
        assert request.url.host == 'gmail.googleapis.com'
        path = request.url.path.removeprefix('/gmail/v1/users/me')
        if request.method == 'GET':
            assert path == '/labels'
            return httpx.Response(200,json={'labels':[{'id':'Label_1','name':'Filed'}]})
        writes.append((request.method,path,json.loads(request.content) if request.content else None))
        return httpx.Response(200,json={})
    with httpx.Client(transport=httpx.MockTransport(exchange)) as http:
        monkeypatch.setattr('lib.service.account_client',
                            lambda store, row, **_: GmailClient(store, row['id'], http=http))
        svc = MailService(SimpleNamespace(get_state_dir=lambda:directory,get_settings=lambda _:{}))
        handlers = {}
        register_tools(SimpleNamespace(register_tool=lambda name,h,**_:handlers.update({name:h})),svc)
        yield account,lambda tool,args:json.loads(handlers[tool](**args)),writes


@pytest.mark.parametrize('cross_account',[False,True])
@pytest.mark.parametrize('tool,args,marker',[
    ('email_mark_read',{'uid':11,'uidvalidity':1,'remove_labels':['UNREAD']},'UNREAD'),
    ('email_mailbox',{'action':'labels','uid':11,'uidvalidity':1,'add_labels':['Label_1'],'label_names':['Filed']},'Label_1'),
    ('email_mailbox',{'action':'trash','uid':11,'uidvalidity':1,'label_names':['TRASH']},'TRASH'),
    ('email_mailbox',{'action':'delete','uid':11,'uidvalidity':1,'label_names':['decoy']},'decoy'),
    ('email_mailbox',{'action':'delete_draft','draft_id':'draft-one','label_names':['decoy']},'decoy'),
])
def test_implicit_account_cannot_borrow_another_active_target(tmp_path,monkeypatch,sole_gmail,cross_account,tool,args,marker):
    account,call,writes=sole_gmail
    guard=host_guard(tmp_path,monkeypatch,tool,account,[('','paused')])
    from ouroboros import places
    for active in set([marker, *args.get('label_names', [])]):
        places.connect_place(tmp_path/'host',connector='email-presence',provider='email',
                             account_id='google:other' if cross_account else account,
                             conversation_id=active,state='active')
    # Unqualified targets alone can resolve Active. The selected account must
    # be returned to the Host before the skill reaches any provider mutation.
    assert not guard(args)
    result=call(tool,args)
    assert result['status']=='needs_arguments' and not writes
    retry=result['retry_arguments']
    assert retry['account_id']==account and 'Paused' in guard(retry) and not writes


@pytest.mark.parametrize('tool,args,targets,method,path,body',[
    ('email_mark_read',{'uid':11,'uidvalidity':1},{'remove_labels':['UNREAD']},'POST','/messages/b/modify',
                      {'addLabelIds':[],'removeLabelIds':['UNREAD']}),
    ('email_mailbox',{'action':'move','folder':'Filed','destination':'STARRED','uid':11,'uidvalidity':1},
     {'folder':'Label_1','label_names':['Filed'],'remove_labels':['Label_1']},'POST','/messages/b/modify',
     {'addLabelIds':['STARRED'],'removeLabelIds':['Label_1']}),
    ('email_mailbox',{'action':'create','properties':{'name':'New'}},{'destination':'New'},'POST','/labels',{'name':'New'}),
    ('email_mailbox',{'action':'delete','uid':11,'uidvalidity':1},{},'DELETE','/messages/b',None),
    ('email_mailbox',{'action':'delete_draft','draft_id':'draft-one'},{'remove_labels':['DRAFT'],'label_names':['Drafts']},'DELETE','/drafts/draft-one',None),
])
def test_sole_active_account_prepares_all_targets_once(tmp_path,monkeypatch,sole_gmail,tool,args,targets,method,path,body):
    account,call,writes=sole_gmail
    guard=host_guard(tmp_path,monkeypatch,tool,account,[('','active')])
    result=call(tool,args)
    assert result['status']=='needs_arguments' and result['code']=='canonical_account_required' and not writes
    retry=result['retry_arguments']
    assert retry['account_id']==account
    assert all(retry[k]==v for k,v in targets.items())
    assert not guard(retry)
    assert call(tool,retry)['account_id']==account and writes==[(method,path,body)]


def test_implicit_folder_lists_remain_reads(sole_gmail):
    account,call,writes=sole_gmail
    for tool,args in [('email_folders',{}),('email_mailbox',{}),('email_mailbox',{'action':'list'})]:
        assert call(tool,args)=={'account_id':account,'folders':[{'id':'Label_1','name':'Filed'}]}
    assert not writes


@pytest.mark.parametrize('provider',['ews','imap'])
@pytest.mark.parametrize('key,value',[('label_names',['decoy']),('add_labels',['decoy']),('remove_labels',['decoy']),('label_id','decoy')])
def test_non_gmail_cannot_advertise_unused_label_targets(tmp_path,provider,key,value):
    class Client:
        def mailbox(self, **args):
            pytest.fail('No provider call may use an unrelated Gmail target')
    account,call=tools_for(tmp_path,provider,Client())
    with pytest.raises(ValueError,match='only to Gmail'):
        call('email_mailbox',{'account_id':account,'action':'create','destination':'New',key:value})


def test_implicit_gmail_retry_preserves_opaque_public_reference(sole_gmail):
    account,call,writes=sole_gmail
    ref=MailService.wrap_ref({'id':account},{'message_ref':'gmail:abcdef'})
    args={'action':'labels','message_ref':ref,'add_labels':['Label_1']}
    retry=call('email_mailbox',args)['retry_arguments']
    assert retry['message_ref']==ref and retry['account_id']==account and retry['label_names']==['Filed'] and not writes
    assert call('email_mailbox',retry)['account_id']==account
    assert writes==[('POST','/messages/abcdef/modify',{'addLabelIds':['Label_1'],'removeLabelIds':[]})]


@pytest.mark.parametrize('fallback',[False,True])
@pytest.mark.parametrize(('status','payload','hint'),[
    (403,{'code':'presence_auth_blocked'},'access in Skills'),
    (403,{'code':'different_refusal'},'unavailable'),
    (503,{'code':'places_unavailable'},'unavailable'),
    (200,{'allowed':False,'binding':None,'state':'paused','label':'Work mail'},'Work mail is paused'),
])
def test_draft_folder_failure_explains_draft_without_promising_queued_send(fallback,status,payload,hint):
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter
    def answer(req):
        if req.url.path == '/identity':
            return httpx.Response(200,json={'places_version':1,'presence_place_binding_version':1,'presence_delivery_version':1})
        if fallback and req.url.params.get('operation') == 'bind':
            return httpx.Response(200,json={'allowed':False,'binding':None,'code':'place_unattributable'})
        return httpx.Response(status,json=payload)
    with httpx.Client(transport=httpx.MockTransport(answer)) as http:
        adapter=LoopbackPresenceHostAdapter('',host_service_url='http://127.0.0.1:8767',skill_token='fixture',
                                            place_http_client=http,sync_only=True)
        error=adapter.draft_folder_hold('imap:test','Work')
    assert error.startswith('Draft not saved:') and hint in error
    assert all(term not in error.lower() for term in ('sent','send','by itself','approval'))
