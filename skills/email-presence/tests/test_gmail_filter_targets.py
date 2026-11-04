"""A stored Gmail rule runs later at Google. Check its real destinations before creating it."""
import json

import pytest

from test_declared_target_preparation import published_targets
from test_write_targets import gmail_for, host_guard

pytest_plugins = ["test_write_targets"]


def rule():
    return {
        'criteria': {'query':'has:attachment','from':'team@example.invalid','negatedQuery':'after:2025/01/01',
                     'size':800,'sizeComparison':'larger','excludeChats':True},
        'action': {'addLabelIds':['Label_1'], 'removeLabelIds':['INBOX'], 'forward':'archive@example.invalid'},
    }


@pytest.mark.parametrize('target', ['Label_1','Filed','INBOX','archive@example.invalid'])
@pytest.mark.parametrize('state', ['silent','active'])
def test_google_receives_exact_rule_only_after_host_checks_its_action(tmp_path, monkeypatch, sole_gmail, target, state):
    account, call, writes = sole_gmail
    guard = host_guard(tmp_path, monkeypatch, 'email_settings', account, [('', 'active'),(target, state)])
    body = rule()
    original = json.dumps(body, sort_keys=True)
    args = {'account_id':account, 'resource':'filters','action':'create','values':body,
            'forward_to':'decoy@example.invalid', 'add_labels':['OTHER'], 'remove_labels':[], 'label_names':['Other']}
    assert not guard(args)
    first = call('email_settings', args)
    assert first['status']=='needs_arguments' and not writes
    retry = first['retry_arguments']
    assert published_targets('email_settings',retry)=={
        'account_id':[account], 'forward_to':['archive@example.invalid'], 'add_labels':['Label_1'],
        'remove_labels':['INBOX'], 'label_names':['Filed']}
    assert retry['values']==body and json.dumps(body,sort_keys=True)==original
    denial = guard(retry)
    if state=='silent':
        assert denial and not writes
    else:
        assert not denial
        call('email_settings', retry)
        assert writes==[('POST','/settings/filters',body)]


@pytest.mark.parametrize('state',['paused','active'])
@pytest.mark.parametrize('cross_account',[True,False])
def test_sole_account_and_all_rule_targets_share_one_retry(tmp_path,monkeypatch,sole_gmail,state,cross_account):
    account,call,writes=sole_gmail
    guard=host_guard(tmp_path,monkeypatch,'email_settings',account,[('',state)])
    from ouroboros import places
    places.connect_place(tmp_path/'host',connector='email-presence',provider='email',
                         account_id='google:other' if cross_account else account,
                         conversation_id='archive@example.invalid',state='active')
    body=rule()
    args={'resource':'filters','action':'create','values':body,'forward_to':'archive@example.invalid'}
    assert not guard(args)
    first=call('email_settings',args)
    assert first['code']=='canonical_account_required' and not writes
    retry=first['retry_arguments']
    assert retry['account_id']==account and retry['label_names']==['Filed'] and retry['remove_labels']==['INBOX']
    assert retry['values']==body
    if state=='paused':
        assert 'Paused' in guard(retry) and not writes
    else:
        assert not guard(retry)
        call('email_settings',retry)
        assert writes==[('POST','/settings/filters',body)]


@pytest.mark.parametrize('state',['active','paused'])
def test_stale_label_name_rechecks_current_target_without_rebinding_id(tmp_path,monkeypatch,state):
    labels=[{'id':'Label_1','name':'Previous'}]
    reads=[]
    account,call,writes=gmail_for(tmp_path,label_rows=labels,reads=reads)
    guard=host_guard(tmp_path,monkeypatch,'email_settings',account,[('','active'),('New name',state)])
    body=rule()
    args={'account_id':account,'resource':'filters','action':'create','values':body}
    retry=call('email_settings',args)['retry_arguments']
    assert retry['label_names']==['Previous'] and reads==['/labels'] and not writes
    labels[:]=[{'id':'Label_1','name':'New name'},{'id':'Label_2','name':'Previous'}]
    assert not guard(retry)
    current=call('email_settings',retry)['retry_arguments']
    assert current['label_names']==['New name'] and current['add_labels']==['Label_1'] and not writes
    if state=='paused':
        assert 'Paused' in guard(current)
    else:
        assert not guard(current)
        call('email_settings',current)
        assert writes==[('POST','/settings/filters',body)]
    assert reads==['/labels']*(2 if state=='paused' else 3)
    labels.clear()
    with pytest.raises(ValueError,match='current Gmail label IDs'):
        call('email_settings',current)
    assert len(writes)==(state=='active')


@pytest.mark.parametrize(('args','method','path','written'), [
    ({'resource':'language','values':{'displayLanguage':'en'}},'PUT','/settings/language',{'displayLanguage':'en'}),
    ({'resource':'filters','action':'delete','identifier':'f1','values':rule()},'DELETE','/settings/filters/f1',None),
])
def test_settings_and_deleting_a_rule_check_account_but_do_not_execute_filter_actions(sole_gmail,args,method,path,written):
    account,call,writes=sole_gmail
    result=call('email_settings',args)
    assert result['code']=='canonical_account_required' and not writes
    retry=result['retry_arguments']
    assert retry=={**args,'account_id':account}
    call('email_settings',retry)
    assert writes==[(method,path,written)]
    with pytest.raises(ValueError,match='only to creating'):
        call('email_settings',{**retry,'label_names':['Unrelated']})
    assert len(writes)==1


@pytest.mark.parametrize("values,extra", [
    ({"action":{"add_label_ids":["Label_1"]}},{}),
    ({"Action":{"forward":"hidden@example.invalid"}},{}),
    ({"action":{"forward":False}},{}),
    ({"action":{"removeLabelIds":"INBOX"}},{}),
    ({"action":{"addLabelIds":[0]}},{}),
    ({"action":[]},{}),
    (rule(),{"identifier":"filter-id"}),
])
def test_filter_cannot_post_an_action_or_path_its_preparation_did_not_cover(sole_gmail,values,extra):
    account,call,writes=sole_gmail
    args={"account_id":account,"resource":"filters","action":"create","values":values,**extra}
    with pytest.raises(ValueError):
        call("email_settings",args)
    assert writes==[]


def test_descriptive_labels_cannot_add_an_unrequested_effect(sole_gmail):
    account,call,writes=sole_gmail
    # Purely system labels need no metadata lookup; forwarded fields cannot inject custom labels.
    body={"criteria":{"query":"is:unread"},"action":{"removeLabelIds":["UNREAD"]}}
    args={"account_id":account,"resource":"filters","action":"create","values":body,
          "remove_labels":["UNREAD"],"add_labels":["TRASH"],"label_names":["Invented"]}
    retry=call("email_settings",args)["retry_arguments"]
    assert retry["add_labels"]==[] and retry["label_names"]==[] and not writes
    call("email_settings",retry)
    assert writes==[("POST","/settings/filters",body)]
