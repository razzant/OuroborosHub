"""Declared target facts must describe the actual provider body, without new policy."""
import json,os,sys
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
import pytest,yaml
SKILL=Path(__file__).resolve().parents[1]
from lib.service import MailService,register_tools
from lib.gmail import GmailClient
from lib.client import MailClient
MANIFEST=yaml.safe_load((SKILL/'SKILL.md').read_text(encoding='utf-8').split('---',2)[1])
DECLARATIONS={row['name']:row for row in MANIFEST['tools']}


def published_targets(name,args):
    source=os.environ.get('OUROBOROS_SOURCE_DIR')
    if not source:pytest.skip('Set OUROBOROS_SOURCE_DIR for the real Host target projection')
    if source not in sys.path:sys.path.insert(0,source)
    from ouroboros.tool_effects import ToolEffect,target_values
    row=DECLARATIONS[name]
    return target_values(ToolEffect(row['effect'],tuple(row.get('target') or ()),row.get('conversation',''),row.get('thread',''),True),args)


def harness(provider='gmail'):
    requests=[];account={'id':provider+':fixture','provider':provider,'address':'owner@example.invalid'}
    if provider=='gmail':
        client=GmailClient.__new__(GmailClient)
        def capture(method,path,**kwargs):
            if method=='GET' and path=='/labels':
                return {'labels':[{'id':'Label_1','name':'Filed'}]}
            requests.append({'method':method,'path':path,**kwargs})
            return {'id':'fixture-result'}
        client._request=capture
    else:
        client=MailClient.__new__(MailClient)
        class Box:
            capabilities=(b'IMAP4rev1',b'MOVE')
            def create(self,target):requests.append({'create':target});return 'OK',[b'created']
            def uid(self,*args):requests.append({'uid':args});return 'OK',[b'done']
        @contextmanager
        def imap(*args,**kwargs):yield Box()
        client.imap=imap
        client.metadata=lambda box:(7,2)
    service=MailService.__new__(MailService)
    @contextmanager
    def target(account_id=''):
        assert account_id==account['id']
        yield account,None,client
    service.target=target
    registered={}
    schemas={}
    def register(name,handler,**kw):
        registered[name]=handler
        schemas[name]=kw['schema']
    register_tools(SimpleNamespace(register_tool=register),service)
    return account,registered,requests,schemas


def invoke(registered,requests,name,args,schemas):
    result=json.loads(registered[name](**args));calls=1
    if result.get('status')=='needs_arguments':
        assert requests==[],'Preparation must precede the provider effect'
        args=result['retry_arguments']
        import jsonschema
        jsonschema.validate(args,schemas[name])
        result=json.loads(registered[name](**args));calls+=1
    assert len(requests)==1
    return result,args,calls,published_targets(name,args)


@pytest.mark.parametrize('shape',['direct','properties_name'])
def test_mailbox_target_matches_effective_gmail_create_name(shape):
    account,registered,requests,schemas=harness()
    properties={'labelListVisibility':'labelShow','messageListVisibility':'show'}
    if shape=='properties_name':properties['name']='Actual nested name'
    args={'account_id':account['id'],'action':'create','destination':'Top-level name','properties':properties}
    expected={'name':'Top-level name',**properties}
    result,used,calls,targets=invoke(registered,requests,'email_mailbox',args,schemas)
    print('TARGET_MAILBOX',json.dumps({'shape':shape,'calls':calls,'targets':targets,'request':requests[0]}))
    assert requests[0]=={'method':'POST','path':'/labels','body':expected}
    assert DECLARATIONS['email_mailbox']['effect']=='write'
    assert expected['name'] in [value for values in targets.values() for value in values]
    if shape=='direct':assert calls==1


def test_imap_create_destination_remains_supported_and_attributed():
    account,registered,requests,schemas=harness('imap')
    name='Архив для писем'
    result,used,calls,targets=invoke(registered,requests,'email_mailbox',{'account_id':account['id'],'action':'create','destination':name},schemas)
    print('TARGET_IMAP',json.dumps({'targets':targets,'request':requests[0]}))
    assert requests==[{'create':MailClient.folder_name(name)}] and calls==1 and result['ok']
    assert name in [value for values in targets.values() for value in values]


@pytest.mark.parametrize('forward',[False,True])
def test_filter_body_is_preserved_and_forward_destination_is_attributed(forward):
    account,registered,requests,schemas=harness()
    body={'criteria':{'from':'sender@example.invalid'},'action':{'addLabelIds':['Label_1'],'removeLabelIds':['INBOX']}}
    if forward:body['action']['forward']='forward@example.invalid'
    result,used,calls,targets=invoke(registered,requests,'email_settings',{'account_id':account['id'],'resource':'filters','action':'create','values':body},schemas)
    print('TARGET_FILTER',json.dumps({'forward':forward,'calls':calls,'targets':targets,'request':requests[0]}))
    assert requests==[{'method':'POST','path':'/settings/filters','body':body}]
    assert DECLARATIONS['email_settings']['effect']=='write'
    if forward:assert body['action']['forward'] in [value for values in targets.values() for value in values]
    assert calls==2
    assert targets['account_id']==[account['id']]
    assert targets['add_labels']==['Label_1'] and targets['remove_labels']==['INBOX']
    assert targets['label_names']==['Filed']


@pytest.mark.parametrize('matching',[False,True])
def test_gmail_name_only_preparation_keeps_properties_and_matching_call_is_direct(matching):
    account,registered,requests,schemas=harness()
    properties={'name':'Actual label','color':{'backgroundColor':'#ffffff','textColor':'#000000'}}
    args={'account_id':account['id'],'action':'create','properties':properties}
    if matching:args['destination']='Actual label'
    result,used,calls,targets=invoke(registered,requests,'email_mailbox',args,schemas)
    assert calls==(1 if matching else 2)
    assert requests[0]['body']==properties
    assert used['properties']==properties and targets['destination']==['Actual label']


@pytest.mark.parametrize('shape',['nested_only','matching','mismatch','top_only'])
def test_filter_target_preparation_preserves_values_and_never_sends_mismatch(shape):
    account,registered,requests,schemas=harness()
    values={'criteria':{'query':'has:attachment','from':'sender@example.invalid'},
            'action':{'addLabelIds':['Label_1'],'removeLabelIds':['INBOX']}}
    if shape!='top_only':values['action']['forward']='actual@example.invalid'
    original=json.dumps(values,sort_keys=True)
    args={'account_id':account['id'],'resource':'filters','action':'create','values':values,
          'add_labels':['Label_1'],'remove_labels':['INBOX'],'label_names':['Filed']}
    if shape in ('matching','top_only'):args['forward_to']='actual@example.invalid'
    if shape=='mismatch':args['forward_to']='wrong@example.invalid'
    result,used,calls,targets=invoke(registered,requests,'email_settings',args,schemas)
    assert calls==(2 if shape in ('nested_only','mismatch') else 1)
    expected={**values,'action':{**values['action'],'forward':'actual@example.invalid'}}
    assert requests[0]['body']==expected
    assert targets['forward_to']==['actual@example.invalid']
    assert json.dumps(values,sort_keys=True)==original


@pytest.mark.parametrize('provider',['gmail','imap'])
@pytest.mark.parametrize('action',['copy','move'])
def test_existing_copy_move_targets_stay_one_call(provider,action):
    account,registered,requests,schemas=harness(provider)
    destination='STARRED' if provider=='gmail' else 'Archive'
    result,used,calls,targets=invoke(registered,requests,'email_mailbox',{
        'account_id':account['id'],'action':action,'folder':'INBOX','uid':4,
        'uidvalidity':1 if provider=='gmail' else 7,'destination':destination,
        **({'remove_labels':['INBOX']} if provider=='gmail' and action=='move' else {})},schemas)
    assert calls==1 and targets['destination']==[destination]
    if provider=='gmail':
        assert requests[0]=={'method':'POST','path':'/messages/4/modify','body':{
            'addLabelIds':['STARRED'],'removeLabelIds':['INBOX'] if action=='move' else []}}
    else:assert requests[0]=={'uid':(action.upper(),'4',MailClient.folder_name(destination))}


@pytest.mark.parametrize('resource,action,identifier,values,method,path',[
    ('filters','delete','filter-id',{'action':{'forward':'irrelevant@example.invalid'}},'DELETE','/settings/filters/filter-id'),
    ('language','update','',{'displayLanguage':'en'},'PUT','/settings/language'),
])
def test_nonforwarding_effects_do_not_acquire_a_new_argument(resource,action,identifier,values,method,path):
    account,registered,requests,schemas=harness()
    result,used,calls,targets=invoke(registered,requests,'email_settings',{
        'account_id':account['id'],'resource':resource,'action':action,'identifier':identifier,'values':values},schemas)
    assert calls==1 and targets=={'account_id':[account['id']]}
    assert requests==[{'method':method,'path':path,'body':None if action=='delete' else values}]


@pytest.mark.parametrize('name',['email_mailbox','email_settings'])
@pytest.mark.parametrize('place_state',['no_place','active','silent','paused','advises'])
def test_canonical_target_metadata_preserves_real_host_write_modes(tmp_path,monkeypatch,name,place_state):
    source=os.environ.get('OUROBOROS_SOURCE_DIR')
    if not source:pytest.skip('Set OUROBOROS_SOURCE_DIR for the real Host guard')
    monkeypatch.syspath_prepend(source)
    from ouroboros import extension_registry_state as registry,place_guard,places
    from ouroboros.extension_surface_names import extension_surface_name
    from ouroboros.consciousness_authority import CONSCIOUSNESS_INITIATOR
    descriptor={'skill':'email-presence','extension_generation':'target-test'}
    bundle=registry._ExtensionRegistrations(generation_digest='target-test',loaded_skill=SimpleNamespace(manifest=SimpleNamespace(raw_extra=MANIFEST)))
    full=extension_surface_name('email-presence',name)
    monkeypatch.setitem(registry._extensions,'email-presence',bundle)
    monkeypatch.setitem(registry._tools,full,descriptor)
    drive=tmp_path/'host'
    if place_state!='no_place':
        places.connect_place(drive,connector='email-presence',provider='email',account_id='gmail:fixture',
                             state='active' if place_state=='advises' else place_state)
    metadata={'initiator':CONSCIOUSNESS_INITIATOR,'consciousness_autonomy':'observe'} if place_state=='advises' else {}
    ctx=SimpleNamespace(drive_root=drive,task_id='ordinary-task',task_metadata=metadata)
    args={'account_id':'gmail:fixture',**({'destination':'Archive'} if name=='email_mailbox' else {'forward_to':'actual@example.invalid'})}
    refusal=place_guard.dispatch_refusal(ctx,full,args,ext_tool=descriptor)
    assert bool(refusal)==(place_state in ('silent','paused','advises'))
    if place_state=='advises':assert 'Advises' in refusal
    facts=place_guard.call_place_facts(ctx,full,args,ext_tool=descriptor)
    if place_state!='no_place':
        assert facts['effect']=='write' and facts['declared']
        assert facts['targets']=={key:[value] for key,value in args.items()}


@pytest.mark.parametrize('prepare', [False, True])
def test_mailbox_destination_retry_keeps_the_public_message_reference(prepare):
    account, registered, requests, schemas = harness('gmail')
    reference = MailService.wrap_ref(account, {'message_ref': 'gmail:message-one'})
    properties = {'name': 'Actual label', 'labelListVisibility': 'labelShow'}
    args = {'account_id': account['id'], 'action': 'create', 'message_ref': reference,
            'destination': 'Initial label' if prepare else 'Actual label', 'properties': properties}
    result = json.loads(registered['email_mailbox'](**args))
    if prepare:
        assert result['status'] == 'needs_arguments' and requests == []
        retry = result['retry_arguments']
        assert retry['message_ref'] == reference
        assert retry['properties'] == properties and retry['destination'] == properties['name']
        import jsonschema
        jsonschema.validate(retry, schemas['email_mailbox'])
        result = json.loads(registered['email_mailbox'](**retry))
    assert result['account_id'] == account['id']
    assert requests == [{'method': 'POST', 'path': '/labels', 'body': properties}]
