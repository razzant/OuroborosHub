"""Canonical lookup preserves old identity keys; legacy and emitted conversation holds are independent."""
from contextlib import contextmanager
from dataclasses import replace

import pytest

from lib.client import MailClient
from lib.ews import EwsClient
from lib.gmail import GmailClient
from lib.mime import parse_message
from lib.runtime import EmailRuntime
from lib.service import MailService
from lib.store import EmailStore
from test_binding_core import DeliveryClient, release_hold
from test_mutation_identity_authority import ROOT_OF, thread_place
from test_reply_source_authority import service_for, source
from test_write_targets import host_guard

pytest_plugins = ['test_binding_core']

TOKEN = '<source@example.invalid>'
COMMENT = TOKEN + ' (sent by an old client)'
LEGACY = '<a@example.invalid> <b@example.invalid>'
EMITTED = '<a@example.invalid>'


def configure_host(monkeypatch, core_host, account, roots):
    places, drive, adapter = core_host
    @contextmanager
    def host(self):
        yield adapter
    monkeypatch.setattr(MailService, '_presence_host', host)
    places.connect_place(drive, connector='email-presence', provider='email', account_id=account, state='active')
    for root, state in roots:
        thread_place(drive, account, root, root, state)
    return drive, adapter


def invoke(call, tool, args, guard=lambda args: ''):
    for _ in range(5):
        refusal = guard(args)
        if refusal:
            return args, {'refusal': refusal}
        result = call(tool, args)
        if result.get('status') != 'needs_arguments':
            return args, result
        args = result['retry_arguments']
    pytest.fail('preparation did not converge')


@pytest.mark.parametrize('provider', ['imap', 'gmail', 'ews'])
@pytest.mark.parametrize('where', ['provider', 'remembered', 'captured'])
@pytest.mark.parametrize('spelling', [TOKEN, COMMENT])
@pytest.mark.parametrize('tool', ['email_send', 'email_draft'])
@pytest.mark.parametrize('reply_all', [False, True])
def test_commented_message_id_preserves_source_and_replies(tmp_path, provider, where, spelling, tool, reply_all):
    original = source(COMMENT, references=[], recipients=['owner@example.invalid', 'copy@example.invalid'])
    account, call, store, client = service_for(tmp_path, provider,
        captured=[original] if where == 'captured' else ())
    if where == 'remembered':
        store.remember_read(original)
    if where == 'provider':
        client.find_message = lambda message_id, **kw: dict(original)  # raw verification is tested separately below
    args, done = invoke(call, tool, {'account_id': account, 'to': 'peer@example.invalid', 'body': 'Answer',
        'subject': 'Re: Source', 'reply_to_message_id': spelling, 'reply_all': reply_all, 'request_id': 'r'})
    assert done['ok']
    assert args['reply_to_message_id'] == COMMENT
    assert args['conversation_id'] == args['thread_id'] == COMMENT
    assert args['emitted_conversation_id'] == TOKEN
    assert store.message_by_id(TOKEN)['message_id'] == store.message_by_id(COMMENT)['message_id'] == COMMENT
    if tool == 'email_send':
        queued = store.queued_message('r')
        assert queued.in_reply_to == COMMENT
        wire = MailClient({}).message(queued)
        assert ROOT_OF(parse_message(wire.as_bytes(), folder='', uid=0)) == TOKEN
        assert bool(queued.cc) == reply_all
        before = queued
        assert call(tool, args)['deduplicated']
        assert store.queued_message('r') == before
    else:
        assert client.saved[-1]['reply_to_message_id'] == COMMENT
        assert bool(client.saved[-1]['cc']) == reply_all


@pytest.mark.parametrize('provider', ['imap', 'gmail', 'ews'])
@pytest.mark.parametrize('spelling', [TOKEN, COMMENT])
@pytest.mark.parametrize('duplicate', ['none', 'copy', 'conflict'])
def test_provider_lookup_pages_and_verifies_raw_identity(provider, spelling, duplicate):
    original = source(COMMENT, references=[])
    second = {**original, **({'references': ['<different@example.invalid>']} if duplicate == 'conflict' else {})}
    candidates = {'one': original, 'two': second}
    pages = [[{'uid': 'one', 'uidvalidity': 1, 'message_ref': 'one', 'message_id': TOKEN}]]
    if duplicate != 'none':
        pages.append([{'uid': 'two', 'uidvalidity': 1, 'message_ref': 'two', 'message_id': TOKEN}])
    read_ids, queries = [], []
    def search(**kwargs):
        queries.append(kwargs)
        rows = pages.pop(0)
        return {'messages': rows, 'next_page_token': '1' if pages else ''}
    def read(*, uid=None, message_ref=None, **kw):
        read_ids.append(uid or message_ref)
        return dict(candidates[uid or message_ref])
    if provider == 'imap':
        client = MailClient({}); client.search = search; client.read = read
    elif provider == 'gmail':
        client = GmailClient.__new__(GmailClient); client.search = search; client.read = read
    else:
        client = EwsClient.__new__(EwsClient)
        client._find = lambda folder, limit, offset, **kw: search(folder=folder, offset=offset, **kw)
        client._read_scoped = read
    if duplicate == 'conflict':
        with pytest.raises(ValueError, match='Ambiguous reply source'):
            client.find_message(spelling)
    else:
        assert client.find_message(spelling)['message_id'] == COMMENT
    assert read_ids == (['one'] if duplicate == 'none' else ['one', 'two'])
    assert len(queries) == len(read_ids)


@pytest.mark.parametrize('where', ['read', 'inbox', 'mixed'])
@pytest.mark.parametrize('duplicate', ['copy', 'conflict'])
def test_store_alias_ambiguity_never_rewrites_keys(tmp_path, where, duplicate):
    store = EmailStore(tmp_path)
    original = source(COMMENT, references=[], recipients=[])
    other = source(TOKEN if duplicate == 'conflict' else COMMENT,
                   references=['<different@example.invalid>'] if duplicate == 'conflict' else [], recipients=[])
    store.ingest({**original, 'folder': 'A', 'uid': 1, 'uidvalidity': 1})
    if where in ('read', 'mixed'):
        store.remember_read(other)
    if where in ('inbox', 'mixed'):
        store.ingest({**other, 'folder': 'B', 'uid': 2, 'uidvalidity': 1})
    with store._connect() as db:
        before = list(db.execute('SELECT message_id,context_json FROM inbox'))
    if duplicate == 'conflict':
        with pytest.raises(ValueError, match='Ambiguous reply source'):
            store.message_by_id(TOKEN)
    else:
        assert store.message_by_id(TOKEN)['message_id'] == COMMENT
    with store._connect() as db:
        assert [tuple(r) for r in db.execute('SELECT message_id,context_json FROM inbox')] == [tuple(r) for r in before]


@pytest.mark.parametrize('provider', ['imap', 'gmail', 'ews'])
@pytest.mark.parametrize('tool', ['email_send', 'email_draft'])
@pytest.mark.parametrize('source_state,emitted_state', [('active','active'), ('active','paused'), ('active','silent'),
                                                      ('paused','active'), ('silent','active')])
def test_legacy_reply_checks_source_and_emitted_before_effect(tmp_path, monkeypatch, core_host, provider, tool,
                                                            source_state, emitted_state):
    original = source(TOKEN, references=[], in_reply_to=LEGACY)
    account, call, store, client = service_for(tmp_path, provider, captured=[original])
    drive, adapter = configure_host(monkeypatch, core_host, account, [(LEGACY,source_state), (EMITTED,emitted_state)])
    guard = host_guard(tmp_path, monkeypatch, tool, account, [], drive=drive)
    args, result = invoke(call, tool, {'account_id': account, 'reply_to_message_id': TOKEN,
        'conversation_id': LEGACY, 'thread_id': LEGACY, 'to': 'peer@example.invalid', 'subject': 'Re: Source',
        'body': 'Answer', 'request_id': 'r'}, guard)
    active = source_state == emitted_state == 'active'
    if tool == 'email_draft':
        assert bool(client.saved) == active
    elif result.get('ok'):
        queued = store.queued_message('r')
        assert queued.reporting['place_binding']['conversation_id'] == LEGACY
        assert queued.reporting['emitted_place_binding']['conversation_id'] == EMITTED
        delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
        runtime = EmailRuntime(store, delivery, adapter, account=account, account_loader=lambda: {'sending':'on'})
        release_hold(store); assert runtime.process_outbox()
        assert bool(delivery.sent) == active
        assert store.receipt('r')['state'] == ('completed' if active else 'held')
        assert call(tool, args)['deduplicated']
    else:
        assert not active and not store.queued_message('r')
    if active:
        assert result.get('ok') and args['emitted_conversation_id'] == EMITTED


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('old_row', [False, True])
@pytest.mark.parametrize('held_root', [LEGACY, EMITTED])
@pytest.mark.parametrize('state', ['paused', 'silent'])
def test_delivery_rechecks_both_roots_and_retains_old_rows(tmp_path, monkeypatch, core_host, automatic, old_row, held_root, state):
    original = source(TOKEN, references=[], in_reply_to=LEGACY)
    account, call, store, _ = service_for(tmp_path, 'imap', captured=[original])
    drive, adapter = configure_host(monkeypatch, core_host, account, [(LEGACY,'active'), (EMITTED,'active')])
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, adapter, account=account, account_loader=lambda: {'sending':'on'})
    if automatic:
        captured = store.claim_inbox()
        runtime.remember_reply_targets(captured)
        runtime._reply(captured, ['Answer'], 'one')
        rid = 'presence:'+captured.provider_event_key+':one'
    else:
        args, result = invoke(call, 'email_send', {'account_id':account, 'reply_to_message_id':TOKEN,
             'to':'peer@example.invalid', 'body':'Answer', 'request_id':'r'})
        assert result['ok']; rid = 'r'
    if old_row:
        import json
        with store._connect() as db:
            facts = json.loads(db.execute('SELECT reporting_json FROM outbox WHERE request_id=?',(rid,)).fetchone()[0])
            # A genuine predecessor row has neither emitted binding nor the new modes/recipient records.
            # Removing just one binding from a new-format row would test corruption instead of compatibility.
            for key in ('emitted_place_binding', 'place_match', 'recipient_places', 'reply_targets'):
                facts.pop(key, None)
            db.execute('UPDATE outbox SET reporting_json=? WHERE request_id=?',(json.dumps(facts),rid))
    before = store.queued_message(rid)
    places, _, _ = core_host
    row = next(p for p in places.load_places(drive) if p.conversation_id == held_root)
    places.edit_place(drive, row.place_id, 'set_state', author='owner', state=state)
    assert runtime.process_outbox() and not delivery.sent
    assert store.receipt(rid)['state'] == 'held'
    assert store.queued_message(rid).reporting == before.reporting
    places.edit_place(drive, row.place_id, 'set_state', author='owner', state='active')
    release_hold(store)
    assert runtime.process_outbox()
    if automatic and old_row:
        from lib.host_adapter import LEGACY_REPLY_HELD
        assert not delivery.sent and store.receipt(rid)['last_error'] == LEGACY_REPLY_HELD
        assert store.queued_message(rid) == before
        runtime._reply(captured, ['A changed duplicate'], 'one')
        assert store.queued_message(rid) == before and not delivery.sent
        return
    assert len(delivery.sent) == 1 and store.receipt(rid)['state'] == 'completed'
    if automatic:
        runtime._reply(captured, ['A changed duplicate'], 'one')
        assert not runtime.process_outbox() and len(delivery.sent) == 1


@pytest.mark.parametrize('provider', ['imap','gmail','ews'])
@pytest.mark.parametrize('tool', ['email_send','email_draft'])
@pytest.mark.parametrize('held_root', [COMMENT,TOKEN])
@pytest.mark.parametrize('state', ['paused','silent'])
def test_comment_alias_keeps_both_original_and_emitted_authority(tmp_path, monkeypatch, core_host, provider, tool, held_root, state):
    original = source(COMMENT, references=[])
    account,call,store,client = service_for(tmp_path,provider,captured=[original])
    drive,adapter = configure_host(monkeypatch,core_host,account,
                                  [(COMMENT,state if held_root==COMMENT else 'active'),
                                   (TOKEN,state if held_root==TOKEN else 'active')])
    guard = host_guard(tmp_path,monkeypatch,tool,account,[],drive=drive)
    args,result = invoke(call,tool,{'account_id':account,'reply_to_message_id':TOKEN,
        'to':'peer@example.invalid','body':'Answer','subject':'Re: Source','request_id':'r'},guard)
    assert not client.saved
    if result.get('ok'):
        assert tool=='email_send' and store.receipt('r')['state']=='held'
        delivery=DeliveryClient({'EMAIL_USER':'owner@example.invalid'})
        runtime=EmailRuntime(store,delivery,adapter,account=account,account_loader=lambda:{'sending':'on'})
        release_hold(store);assert runtime.process_outbox() and not delivery.sent
    else:
        assert result.get('refusal') or result.get('code')=='draft_folder_held'


@pytest.mark.parametrize('held_root', [LEGACY, EMITTED])
@pytest.mark.parametrize('state', ['paused','silent'])
def test_automatic_reply_is_held_at_enqueue_when_either_root_is_held(tmp_path,monkeypatch,core_host,held_root,state):
    account,_,store,_=service_for(tmp_path,'imap',captured=[source(TOKEN,references=[],in_reply_to=LEGACY)])
    _,adapter=configure_host(monkeypatch,core_host,account,
                            [(LEGACY,state if held_root==LEGACY else 'active'),
                             (EMITTED,state if held_root==EMITTED else 'active')])
    delivery=DeliveryClient({'EMAIL_USER':'owner@example.invalid'})
    runtime=EmailRuntime(store,delivery,adapter,account=account,account_loader=lambda:{'sending':'on'})
    captured=store.claim_inbox();runtime._reply(captured,['Answer'],'one')
    rid='presence:'+captured.provider_event_key+':one'
    assert store.receipt(rid)['state']=='held'
    release_hold(store);assert runtime.process_outbox() and not delivery.sent


@pytest.mark.parametrize('root',[LEGACY,EMITTED])
def test_both_new_bindings_keep_disconnect_custody(tmp_path,monkeypatch,core_host,root):
    account,call,store,_=service_for(tmp_path,'imap',captured=[source(TOKEN,references=[],in_reply_to=LEGACY)])
    drive,adapter=configure_host(monkeypatch,core_host,account,[(LEGACY,'active'),(EMITTED,'active')])
    _,result=invoke(call,'email_send',{'account_id':account,'reply_to_message_id':TOKEN,
                                     'to':'peer@example.invalid','body':'Answer','request_id':'r'})
    assert result['ok']
    before=store.queued_message('r').reporting
    places,_,_=core_host
    row=next(p for p in places.load_places(drive) if p.conversation_id==root)
    places.edit_place(drive,row.place_id,'disconnect',author='owner')
    thread_place(drive,account,root,root,'active')
    delivery=DeliveryClient({'EMAIL_USER':'owner@example.invalid'})
    runtime=EmailRuntime(store,delivery,adapter,account=account,account_loader=lambda:{'sending':'on'})
    assert runtime.process_outbox() and not delivery.sent
    assert store.receipt('r')['state']=='held' and store.queued_message('r').reporting==before


@pytest.mark.parametrize('provider',['gmail','ews'])
@pytest.mark.parametrize('held_root',[LEGACY,EMITTED])
@pytest.mark.parametrize('state',['paused','silent'])
def test_legacy_hold_precedes_native_provider_transmission(tmp_path,monkeypatch,core_host,provider,held_root,state):
    import base64
    import json
    import time
    import httpx
    from lib.accounts import AccountStore
    from test_ews_delivery_sync import Exchange, xml, N
    account,call,store,_=service_for(tmp_path,provider,captured=[source(TOKEN,references=[],in_reply_to=LEGACY)])
    drive,adapter=configure_host(monkeypatch,core_host,account,[(LEGACY,'active'),(EMITTED,'active')])
    _,result=invoke(call,'email_send',{'account_id':account,'reply_to_message_id':TOKEN,
                                    'to':'peer@example.invalid','body':'Answer','request_id':'r'})
    assert result['ok']
    transmissions=[]
    if provider=='gmail':
        accounts=AccountStore(tmp_path/'provider')
        # A saved local grant as a sign-in stores it: its client, refresh token and recorded Gmail permissions.
        accounts.save({'id':account,'provider':'gmail','address':'owner@example.invalid','senders':['owner@example.invalid'],
                       'oauth':{'client_id':'fixture-client'},'scopes':['https://www.googleapis.com/auth/gmail.modify'],
                       'tokens':{'access_token':'synthetic','refresh_token':'fixture-refresh','expires_at':time.time()+3600}})
        def send(request):
            assert request.method=='POST' and request.url.path.endswith('/messages/send')
            transmissions.append(base64.urlsafe_b64decode(json.loads(request.content)['raw']+'==='))
            return httpx.Response(200,json={'id':'7b','threadId':'t1'})
        http=httpx.Client(transport=httpx.MockTransport(send))
        client=GmailClient(accounts,account,http=http)
        with store._connect() as db:  # queued through a stand-in provider: record the local access a Gmail client keeps
            db.execute("UPDATE outbox SET reporting_json=json_set(reporting_json,'$.gmail_connection',json(?)) "
                       "WHERE request_id='r'", (json.dumps({'id': accounts.gmail_connection(account)}),))
    else:
        client=Exchange(xml('')); http=None
    runtime=EmailRuntime(store,client,adapter,account=account,account_loader=lambda:{'sending':'on'})
    places,_,_=core_host
    held=next(p for p in places.load_places(drive) if p.conversation_id==held_root)
    places.edit_place(drive,held.place_id,'set_state',author='owner',state=state)
    try:
        assert runtime.process_outbox() and store.receipt('r')['state']=='held'
        assert not transmissions and not getattr(client,'ops',[])
        places.edit_place(drive,held.place_id,'set_state',author='owner',state='active')
        release_hold(store);assert runtime.process_outbox() and store.receipt('r')['state']=='completed'
        if provider=='ews':
            assert len(client.ops)==1 and client.ops[0].get('MessageDisposition')=='SendAndSaveCopy'
            transmissions.append(base64.b64decode(client.ops[0].findtext('m:Items/t:Message/t:MimeContent',namespaces=N)))
        assert len(transmissions)==1
        assert ROOT_OF(parse_message(transmissions[0],folder='',uid=0))==EMITTED
        assert not runtime.process_outbox()
    finally:
        if http:http.close()


@pytest.mark.parametrize('identity', [TOKEN+' (see <another@example.invalid>)', '(old (nested)) '+TOKEN])
def test_legal_comment_syntax_uses_intakes_id_parser_and_keeps_authority(tmp_path,identity):
    from lib.mime import canonical_message_id, usable_message_id
    assert usable_message_id(identity)==identity and canonical_message_id(identity)==TOKEN
    parsed=parse_message(('Message-ID: '+identity+'\r\nFrom: peer@example.invalid\r\n\r\nBody').encode(),folder='INBOX',uid=1)
    assert parsed['message_id']==identity
    account,call,store,_=service_for(tmp_path,'imap')
    store.remember_read(parsed)
    args,done=invoke(call,'email_send',{'account_id':account,'reply_to_message_id':TOKEN,
                  'to':'peer@example.invalid','body':'Answer','request_id':'r'})
    assert done['ok'] and args['conversation_id']==identity
    assert store.queued_message('r').in_reply_to==identity
