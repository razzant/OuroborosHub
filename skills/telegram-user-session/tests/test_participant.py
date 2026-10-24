"""Offline public-consumer checks. No credentials, network, login or live Telegram effects."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from userbot.config import Config,save_config
from userbot.participant_state import Journal,save_policy,policy
from userbot.participant_ops import Operations,SPECS,validate
from userbot.participant import Participant,Host,session_lock
from userbot.telegram import TelegramAdapter
from userbot.participant_plugin import store_asset
from telethon import types as T,functions as F

class Client:
    def __init__(self): self.calls=[]; self.raise_error=None
    async def get_input_entity(self,p):
        if p in ('me',809, 'user'): return T.InputPeerUser(809,99)
        return T.InputPeerChannel(123,456)
    async def get_entity(self,p):
        return T.Channel(id=123,title='Test',photo=T.ChatPhotoEmpty(),date=None,megagroup=True,access_hash=456)
    async def get_messages(self,p,ids=None,**kw):
        doc=T.Document(id=8,access_hash=9,file_reference=b'f',date=None,mime_type='image/webp',size=12,dc_id=1,attributes=[])
        m=NS(id=ids or 10,document=doc,media=T.MessageMediaDocument(document=doc),date=None,message='hello',sender_id=809,peer_id=T.PeerChannel(123))
        return m if ids else [m]
    async def upload_file(self,p): return T.InputFile(1,1,'test.webp','0'*32)
    async def is_user_authorized(self): return True
    async def get_me(self): return T.User(id=8667525238,bot=False)
    async def get_dialogs(self,**kw): return []
    async def delete_messages(self,*a,**kw): return []
    async def __call__(self,r):
        bytes(r)  # Real TL serialization, not a name-only mock.
        self.calls.append(r)
        if self.raise_error: raise self.raise_error
        name=type(r).__name__
        if name=='SearchRequest': return NS(chats=[],users=[])
        if name=='GetParticipantsRequest': return NS(users=[],participants=[],count=0)
        if name=='GetForumTopicsRequest': return NS(topics=[],count=0)
        if name=='GetStickerSetRequest': return NS(set=NS(title='Pack',short_name='test'),documents=[])
        if name=='UploadMediaRequest':
            return T.MessageMediaDocument(document=T.Document(id=8,access_hash=9,file_reference=b'f',date=None,mime_type='image/webp',size=12,dc_id=1,attributes=[]))
        if name in {'SendMediaRequest','SendMessageRequest','ForwardMessagesRequest'}:
            return T.UpdateShortSentMessage(11,1,1,None)
        return True

ARGS={
 'profile':{},'username_check':{'username':'broboros'},'username_set':{'username':'broboros'},
 'profile_update':{'first_name':'Broboros','about':'Ouroboros. AI.'},'avatar_set':{'asset':'ASSET'},
 'dialogs':{},'resolve':{'peer':'-100123'},'search':{'query':'test'},'history':{'peer':'-100123'},
 'members':{'peer':'-100123'},'topics':{'peer':'-100123'},'sticker_pack':{'short_name':'test'},
 'send_text':{'peer':'-100123','text':'hello'},'send_media':{'peer':'-100123','asset':'ASSET'},
 'send_sticker':{'peer':'-100123','source_peer':'-100123','message_id':10},
 'forward':{'peer':'-100123','source_peer':'-100123','message_id':10},
 'edit':{'peer':'-100123','message_id':10,'text':'new'},'delete':{'peer':'-100123','message_id':10},
 'react':{'peer':'-100123','message_id':10,'emoji':'👍'},'join':{'peer':'-100123'},'leave':{'peer':'-100123'},
 'create_chat':{'title':'Test'},'invite':{'peer':'-100123','user':'user'},'ban':{'peer':'-100123','user':'user'},
 'unban':{'peer':'-100123','user':'user'},'mute':{'peer':'-100123','user':'user'},'unmute':{'peer':'-100123','user':'user'},
 'admin':{'peer':'-100123','user':'user','delete_messages':True},'pin':{'peer':'-100123','message_id':10},
 'chat_title':{'peer':'-100123','title':'Test'},'topic_create':{'peer':'-100123','title':'Test'},
 'topic_edit':{'peer':'-100123','topic_id':40,'closed':True},
 'pack_create':{'short_name':'test','title':'Test','source_peer':'-100123','message_id':10,'emoji':'👍'},
 'pack_add':{'short_name':'test','source_peer':'-100123','message_id':10,'emoji':'👍'},
 'pack_remove':{'source_peer':'-100123','message_id':10},'pack_move':{'source_peer':'-100123','message_id':10,'position':0},
 'pack_rename':{'short_name':'test','title':'Test'},'pack_install':{'short_name':'test'},'sticker_upload':{'asset':'ASSET','emoji':'👍'}}

class Adapter(TelegramAdapter):
    async def topic_ids(self,peer): return {1,40}

class HostFake:
    state='active'
    def place(self,*args): return {'place_id':'place','state':self.state}
    def call(self,*args): return 200,{'ok':True,'recorded':True}

class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        save_config(self.root,Config(123,'a'*32,'+1234567890',account_id=8667525238))
        save_policy(self.root,{'mode':'participant','outgoing':True,'expected_account_id':'8667525238'})
        self.client=Client();self.adapter=Adapter(self.root/'unused.session',123,'a'*32,client=self.client)
        self.j=Journal(self.root); self.host=HostFake();self.e=Participant(self.root,self.adapter,self.host,self.j,8667525238)
        self.handle=store_asset(self.root,b'RIFF0000WEBPmock','webp')['asset']
    def tearDown(self):self.j.close();self.tmp.cleanup()
    async def test_all_named_operations_real_serialization(self):
        self.assertEqual(set(ARGS),set(SPECS))
        for op,args in ARGS.items():
            with self.subTest(op=op):
                a=dict(args)
                if a.get('asset')=='ASSET':a['asset']=self.handle
                prepared=await self.e.ops.prepare(op,a)
                result=await self.e.ops.execute(op,prepared,27)
                self.assertIsInstance(result,dict)
    async def test_reaction_is_rpc(self):
        a=await self.e.ops.prepare('react',ARGS['react']);await self.e.ops.execute('react',a,1)
        self.assertIsInstance(self.client.calls[-1],F.messages.SendReactionRequest)
    async def test_profile_uses_actual_rpc_and_hides_phone(self):
        self.client.get_me=lambda:asyncio.sleep(0,result=T.User(id=8667525238,bot=False,first_name='Broboros',username='broboros',phone='private-number'))
        result=await self.e.ops.execute('profile',await self.e.ops.prepare('profile',{}),1)
        self.assertIsInstance(self.client.calls[-1],F.users.GetFullUserRequest)
        self.assertNotIn('phone',result)
        for op,cls in (('username_check',F.account.CheckUsernameRequest),('username_set',F.account.UpdateUsernameRequest),('profile_update',F.account.UpdateProfileRequest),('avatar_set',F.photos.UploadProfilePhotoRequest)):
            args=dict(ARGS[op])
            if args.get('asset')=='ASSET':args['asset']=self.handle
            await self.e.ops.execute(op,await self.e.ops.prepare(op,args),1)
            self.assertIsInstance(self.client.calls[-1],cls)
    async def test_profile_empty_patch_and_paused_write(self):
        with self.assertRaises(ValueError):validate('profile_update',{})
        self.host.state='paused';self.j.enqueue(self.e.account,'profile','profile_update',{'first_name':'Broboros'})
        await self.e.dispatch_one();self.assertEqual(self.client.calls,[])
    async def test_unknown_never_replayed(self):
        self.j.enqueue(self.e.account,'one','send_text',ARGS['send_text'])
        self.client.raise_error=TimeoutError()
        await self.e.dispatch_one();await self.e.dispatch_one()
        self.assertEqual(self.j.get(self.e.account,'one')['state'],'unknown')
        self.assertEqual(len(self.client.calls),1)
    async def test_revocation_after_prepare(self):
        self.j.enqueue(self.e.account,'one','send_text',ARGS['send_text'])
        original=self.e.ops.prepare
        async def prepare(*args):
            result=await original(*args);save_policy(self.root,{'outgoing':False});return result
        self.e.ops.prepare=prepare
        await self.e.dispatch_one()
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.j.get(self.e.account,'one')['state'],'failed')
    async def test_silent_blocks_send(self):
        self.host.state='silent';self.j.enqueue(self.e.account,'one','send_text',ARGS['send_text'])
        await self.e.dispatch_one();self.assertEqual(self.client.calls,[])
    async def test_expected_account(self):
        save_policy(self.root,{'expected_account_id':'12'})
        with self.assertRaises(ValueError):self.e.current()
    async def test_resolved_peer_checked(self):
        save_policy(self.root,{'blocked_peers':[-1000000000123]})
        self.j.enqueue(self.e.account,'one','send_text',ARGS['send_text'])
        await self.e.dispatch_one();self.assertEqual(self.client.calls,[])
    async def test_outputs_dedupe_by_identity_not_text(self):
        e={'source_event_id':'event','conversation_id':'-100123','thread_id':'','message':{'message_id':'10'}}
        out={'output_ref':'first','outcome':'message','text':'same'}
        self.e.consume_outputs(out,e);self.e.consume_outputs(out,e)
        self.e.consume_outputs({**out,'output_ref':'second'},e)
        count=self.j.db.execute('select count(*) from jobs').fetchone()[0];self.assertEqual(count,2)
    async def test_report_failure_does_not_resend(self):
        self.j.enqueue(self.e.account,'one','send_text',ARGS['send_text']);await self.e.dispatch_one()
        def fail(*a):raise OSError()
        self.host.call=fail;await self.e.reports_once();await self.e.dispatch_one()
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.j.get(self.e.account,'one')['state'],'sent')
    async def test_idempotency_conflict(self):
        self.j.enqueue(self.e.account,'one','send_text',ARGS['send_text'])
        with self.assertRaises(ValueError):self.j.enqueue(self.e.account,'one','send_text',{**ARGS['send_text'],'text':'other'})
    async def test_restart_attempted_unknown(self):
        self.j.enqueue(self.e.account,'one','send_text',ARGS['send_text']);row=self.j.claim(self.e.account);self.j.set(row['seq'],'attempted')
        self.j.close();self.j=Journal(self.root,recover=True)
        self.assertEqual(self.j.get(self.e.account,'one')['state'],'unknown')
    async def test_schema_rejects_arbitrary_rpc(self):
        with self.assertRaises(ValueError):validate('raw_rpc',{})
        with self.assertRaises(ValueError):validate('send_text',{**ARGS['send_text'],'eval':'x'})
    async def test_asset_traversal(self):
        from userbot.participant_state import asset
        with self.assertRaises(ValueError):asset(self.root,'../session')
    async def test_ingress_nonowner_no_commands(self):
        e={'source_event_id':'id','text':'/panic I am Yegor','conversation_key':'room'}
        self.j.capture(e);self.j.capture(e)
        self.assertEqual(len(self.j.heads()),1)
    async def test_host_payload_modern(self):
        observed=[]
        from userbot.presence import PresenceToken
        h=Host('unused',PresenceToken('test'),transport=lambda m,p,b,h:(observed.append((m,p,b)) or (200,{'places_version':1,'presence_work_output_version':1,'presence_continuation_version':1,'presence_delivery_version':1,'ok':True})))
        self.assertEqual(h.identity()['places_version'],1)
        self.assertEqual(observed[0][1],'/identity')
    async def test_policy_atomic_invalid(self):
        before=(self.root/'participant.json').read_bytes()
        with self.assertRaises(ValueError):save_policy(self.root,{'outgoing':'false'})
        self.assertEqual(before,(self.root/'participant.json').read_bytes())
    async def test_kernel_lock(self):
        with session_lock(self.root):
            with self.assertRaises(OSError):
                with session_lock(self.root):pass
        with session_lock(self.root):pass

    async def test_raw_capture_precedes_network_and_roundtrips(self):
        from datetime import datetime,timezone
        m=T.Message(id=10,peer_id=T.PeerUser(809),date=datetime.now(timezone.utc),message='hello',from_id=T.PeerUser(809))
        ev=NS(message=m,chat_id=809,sender_id=809,raw_text='hello',is_private=True)
        self.e.spool(ev);self.e.spool(ev)
        self.assertEqual(len(self.j.raw_heads()),1)
        self.assertEqual(self.client.calls,[])
        await self.e.hydrate_once()
        self.assertEqual(len(self.j.raw_heads()),0)
        rows=self.j.heads();self.assertEqual(len(rows),1)
        event=json.loads(rows[0]['event'])
        self.assertIn('direct_message',event['facts'])
        self.assertEqual(event['actor']['platform_actor_id'],'809')
        self.assertNotIn('owner_id_match',event['message'])
    async def test_global_flood_survives_new_job(self):
        self.j.flood(self.e.account,600)
        self.j.enqueue(self.e.account,'new','send_text',ARGS['send_text'])
        self.assertIsNone(self.j.claim(self.e.account))
    async def test_source_revocation_before_read(self):
        save_policy(self.root,{'blocked_peers':[-1000000000123]})
        with self.assertRaises(ValueError):await self.e.ops.prepare('send_sticker',ARGS['send_sticker'])
    async def test_invite_request_is_not_joined(self):
        from telethon.errors import InviteRequestSentError
        a=await self.e.ops.prepare('join',{'peer':'https://t.me/+test'})
        self.client.raise_error=InviteRequestSentError(request=None)
        result=await self.e.ops.execute('join',a,1)
        self.assertEqual(result['membership'],'request_submitted')
        self.assertFalse(result['joined'])

    async def test_excluded_traffic_does_not_kill_permitted_capture(self):
        from datetime import datetime,timezone
        def ev(peer,mid):
            m=T.Message(id=mid,peer_id=T.PeerUser(peer),date=datetime.now(timezone.utc),message='hi',from_id=T.PeerUser(peer))
            return NS(message=m,chat_id=peer,sender_id=peer,raw_text='hi',is_private=True)
        self.e.spool(ev(111,1))
        save_policy(self.root,{'blocked_peers':[111]})
        self.e.spool(ev(111,2));self.e.spool(ev(222,3))
        await self.e.hydrate_once()
        self.assertEqual(len(self.j.raw_heads()),0)
        self.assertEqual(len(self.j.heads()),1)
        self.assertEqual(json.loads(self.j.heads()[0]['event'])['conversation_id'],'222')
    async def test_ambiguous_rpc_classes_unknown(self):
        from telethon import errors
        for n,cls in enumerate((errors.TimedOutError,errors.RandomIdDuplicateError)):
            self.client.raise_error=cls(request=None,message='TIMEOUT') if cls.__name__=='TimedOutError' else cls(request=None)
            self.j.enqueue(self.e.account,'amb'+str(n),'send_text',ARGS['send_text'])
            await self.e.dispatch_one()
            self.assertEqual(self.j.get(self.e.account,'amb'+str(n))['state'],'unknown')
    async def test_invitee_silent_blocks_invitation(self):
        def place(account,peer,topic):return {'place_id':'p','state':'silent' if peer=='809' else 'active'}
        self.host.place=place
        self.j.enqueue(self.e.account,'invite','invite',ARGS['invite']);await self.e.dispatch_one()
        self.assertEqual(self.client.calls,[])
    async def test_revocation_during_source_wait(self):
        self.e.ops.guard=None
        def place(account,peer,topic):
            if peer=='-1000000000123':save_policy(self.root,{'outgoing':False})
            return {'place_id':'p','state':'active'}
        self.host.place=place
        self.j.enqueue(self.e.account,'source','forward',ARGS['forward']);await self.e.dispatch_one()
        self.assertEqual(self.client.calls,[])
    async def test_delete_confined_to_actual_peer(self):
        old=self.client.get_messages
        async def wrong(*a,**kw):
            m=await old(*a,**kw);m.peer_id=T.PeerUser(111);return m
        self.client.get_messages=wrong
        self.j.enqueue(self.e.account,'del','delete',ARGS['delete']);await self.e.dispatch_one()
        self.assertEqual(self.client.calls,[])
    async def test_forward_requires_matching_ack(self):
        async def unknown(r):self.client.calls.append(r);return T.UpdatesTooLong()
        self.client.__class__._normal_call=self.client.__class__.__call__
        self.client.__class__.__call__=lambda c,r:unknown(r)
        try:
            self.j.enqueue(self.e.account,'fwd','forward',ARGS['forward']);await self.e.dispatch_one()
            self.assertEqual(self.j.get(self.e.account,'fwd')['state'],'unknown')
        finally:self.client.__class__.__call__=self.client.__class__._normal_call
    async def test_invite_link_checks_numeric_exclusion(self):
        old=self.client.__class__.__call__
        async def inspect(c,r):
            if isinstance(r,F.messages.CheckChatInviteRequest):
                c.calls.append(r);return NS(chat=await c.get_entity(None))
            return await old(c,r)
        self.client.__class__.__call__=inspect
        try:
            save_policy(self.root,{'blocked_peers':[-1000000000123]})
            self.j.enqueue(self.e.account,'link','join',{'peer':'https://t.me/+invite'});await self.e.dispatch_one()
            self.assertEqual(len(self.client.calls),1)
            self.assertIsInstance(self.client.calls[0],F.messages.CheckChatInviteRequest)
        finally:self.client.__class__.__call__=old
    async def test_reply_wake_uses_actual_sender(self):
        from datetime import datetime,timezone
        old=self.client.get_messages
        async def own(*a,**kw):
            m=await old(*a,**kw);m.sender_id=int(self.e.account);return m
        self.client.get_messages=own
        m=T.Message(id=20,peer_id=T.PeerChannel(123),date=datetime.now(timezone.utc),message='hi',from_id=T.PeerUser(809),reply_to=T.MessageReplyHeader(reply_to_msg_id=10))
        ev=NS(message=m,chat_id=-1000000000123,sender_id=809,raw_text='hi',is_private=False,get_sender=lambda:self.client.get_entity(809),get_chat=lambda:self.client.get_entity(-1000000000123))
        await self.e.capture(ev)
        e=json.loads(self.j.heads()[0]['event']);self.assertIn('reply_to_me',e['facts'])
        self.assertNotIn('social_role',e['conversation'])
    async def test_atomic_send_and_report(self):
        self.j.enqueue(self.e.account,'atomic','send_text',ARGS['send_text']);r=self.j.claim(self.e.account);self.j.set(r['seq'],'attempted')
        self.j.db.execute("CREATE TRIGGER no_report BEFORE INSERT ON reports BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):self.j.settle_with_report(r['seq'],{'message_id':1},'atomic',{'id':1})
        self.assertEqual(self.j.get(self.e.account,'atomic')['state'],'attempted')
        self.j.db.execute('DROP TRIGGER no_report')
        self.j.settle_with_report(r['seq'],{'message_id':1},'atomic',{'id':1})
        self.assertEqual(self.j.get(self.e.account,'atomic')['state'],'sent')
        self.assertEqual(len(self.j.pending_reports()),1)
    async def test_report_backoff_allows_later_receipt(self):
        for n in range(6):self.j.report(str(n),{'n':n})
        for row in self.j.pending_reports():self.j.report_defer(row['id'],'transport')
        self.assertEqual([r['id'] for r in self.j.pending_reports()],['5'])
        self.assertEqual(self.j.counts()['report_errors'],5)

    async def test_topic_places_cannot_be_evaded_by_omission(self):
        old_entity=self.client.get_entity;old_message=self.client.get_messages
        async def entity(p):
            e=await old_entity(p);e.forum=True;return e
        async def message(*a,**kw):
            m=await old_message(*a,**kw);m.reply_to=NS(reply_to_top_id=40,reply_to_msg_id=40,forum_topic=True);m.action=None;return m
        self.client.get_entity=entity;self.client.get_messages=message
        self.host.place=lambda account,peer,topic:{'place_id':'p','state':'paused' if str(topic)=='40' else 'active'}
        for n,op in enumerate(('edit','delete','react','pin','send_text')):
            args=dict(ARGS[op])
            if op=='send_text':args['reply_id']=10
            self.j.enqueue(self.e.account,'topic'+str(n),op,args);await self.e.dispatch_one()
            self.assertEqual(self.j.get(self.e.account,'topic'+str(n))['state'],'failed')
        self.assertEqual(self.client.calls,[])
    async def test_media_revisions_are_immutable_and_host_paths_are_strings(self):
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc)
        async def download(m,file):Path(file).write_bytes(str(m.document.id).encode())
        self.client.download_media=download
        for docid in (1,2):
            doc=T.Document(id=docid,access_hash=9,file_reference=b'f',date=now,mime_type='image/webp',size=1,dc_id=1,attributes=[])
            m=T.Message(id=10,peer_id=T.PeerUser(809),date=now,message='same caption',edit_date=now,from_id=T.PeerUser(809),media=T.MessageMediaDocument(document=doc))
            ev=NS(message=m,chat_id=809,sender_id=809,raw_text='same caption',is_private=True,get_sender=lambda:self.client.get_entity(809),get_chat=lambda:self.client.get_entity(809))
            await self.e.capture(ev,'edit')
        rows=self.j.db.execute('select * from inbox order by seq').fetchall();self.assertEqual(len(rows),2)
        files=[json.loads(r['files'])[0] for r in rows]
        self.assertTrue(all(isinstance(p,str) for p in files));self.assertNotEqual(files[0],files[1])
        self.assertEqual([Path(p).read_bytes() for p in files],[b'1',b'2'])
        requests=[]
        def host(method,path,body=None):requests.append(body);return 200,{'ok':True,'status':'completed','outcome':'silent','text':''}
        self.host.call=host;await self.e.incoming_once()
        self.assertTrue(all(isinstance(p,str) for p in requests[0]['staged_files']))
    async def test_hydration_flood_retains_raw_and_paces_retry(self):
        from datetime import datetime,timezone
        from telethon.errors import FloodWaitError
        m=T.Message(id=10,peer_id=T.PeerUser(809),date=datetime.now(timezone.utc),message='hi',from_id=T.PeerUser(809))
        ev=NS(message=m,chat_id=809,sender_id=809,raw_text='hi',is_private=True)
        self.e.spool(ev)
        async def fail(*args):raise FloodWaitError(request=None,capture=60)
        self.client.get_entity=fail
        await self.e.hydrate_once()
        self.assertEqual(len(self.j.raw_heads()),1)
        self.assertEqual(self.j.counts()['inbound_retrying'],1)
        self.assertGreater(self.j.raw_heads()[0]['due'],0)
        await self.e.hydrate_once()  # Due row does not call the provider again.

    async def test_uncached_sender_profile_does_not_lose_public_message(self):
        from datetime import datetime,timezone
        old=self.client.get_entity
        async def entity(peer):
            if peer==809:raise ValueError('input entity not cached')
            return await old(peer)
        self.client.get_entity=entity
        m=T.Message(id=70,peer_id=T.PeerChannel(123),date=datetime.now(timezone.utc),message='hello from new person',from_id=T.PeerUser(809),mentioned=True)
        self.e.spool(NS(message=m,chat_id=-1000000000123,sender_id=809,raw_text=m.message,is_private=False))
        await self.e.hydrate_once()
        self.assertEqual(len(self.j.raw_heads()),0)
        event=json.loads(self.j.heads()[0]['event'])
        self.assertEqual(event['actor']['platform_actor_id'],'809')
        self.assertEqual(event['text'],'hello from new person')
        self.assertEqual(event['message']['sender_profile_status'],'unavailable')
        self.assertNotIn('is_bot',event['actor'])
        self.assertIn('mention',event['facts'])

    async def test_raw_retirement_and_inbox_publication_are_one_transaction(self):
        self.j.capture_raw('raw',{'peer':809,'actor':809,'kind':'new','private':True},b'raw')
        self.j.db.execute("CREATE TRIGGER no_retire BEFORE UPDATE ON raw_updates BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):
            self.j.capture({'source_event_id':'published','text':'hello'},raw_id='raw')
        self.assertEqual(len(self.j.heads()),0)
        self.assertEqual(len(self.j.raw_heads()),1)
        self.j.db.execute('DROP TRIGGER no_retire')
        self.j.capture({'source_event_id':'published','text':'hello'},raw_id='raw')
        self.assertEqual(len(self.j.heads()),1)
        self.assertEqual(len(self.j.raw_heads()),0)

    async def test_authorized_login_request_retires_without_new_code(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('companion_test',ROOT/'scripts/companion.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        (self.root/'login-request.json').write_text('{"request_code":true}',encoding='utf-8')
        cfg=Config(123,'a'*32,'+1234567890',account_id=8667525238)
        self.assertTrue(await module._login_if_requested(self.root,cfg,self.adapter))
        self.assertFalse((self.root/'login-request.json').exists())
        self.assertEqual(self.client.calls,[])
    async def test_flood_gates_hydration_for_other_peers(self):
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc)
        for n in (1,2):
            m=T.Message(id=n,peer_id=T.PeerUser(n),date=now,message='hi',from_id=T.PeerUser(n))
            self.e.spool(NS(message=m,chat_id=n,sender_id=n,raw_text='hi',is_private=True))
        self.j.flood(self.e.account,100)
        async def forbidden(*a):raise AssertionError('provider read during flood')
        self.client.get_entity=forbidden
        await self.e.hydrate_once();self.assertEqual(len(self.j.raw_heads()),2)

    async def test_login_failure_retains_safe_reason_not_code(self):
        import importlib.util
        from userbot.config import save_login_challenge,stage_login_secret
        from telethon.errors import PhoneCodeInvalidError
        spec=importlib.util.spec_from_file_location('login_diag',ROOT/'scripts/companion.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        async def unauthorized():return False
        async def reject(*a,**kw):raise PhoneCodeInvalidError(request=None)
        self.adapter.authorization_status=unauthorized;self.adapter.complete_login=reject
        save_login_challenge(self.root,'fake-challenge');stage_login_secret(self.root,code='12345')
        with self.assertRaises(PhoneCodeInvalidError):await module._login_if_requested(self.root,Config(123,'a'*32,'+1234567890'),self.adapter)
        saved=(self.root/'login-code-status.json').read_text()
        self.assertIn('PhoneCodeInvalidError',saved);self.assertNotIn('12345',saved)

    async def test_explicit_resend_uses_existing_challenge_once(self):
        import importlib.util
        from userbot.config import save_login_challenge,load_login_challenge,_write_json
        spec=importlib.util.spec_from_file_location('resend_test',ROOT/'scripts/companion.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        save_login_challenge(self.root,'old-challenge')
        _write_json(self.root,'login-request.json',{'request_code':True,'resend':True})
        seen=[]
        async def unauthorized():return False
        async def request(phone,**kw):seen.append(kw);return 'new-challenge'
        self.adapter.authorization_status=unauthorized;self.adapter.request_login_code=request
        cfg=Config(123,'a'*32,'+1234567890')
        self.assertFalse(await module._login_if_requested(self.root,cfg,self.adapter))
        self.assertEqual(seen,[{'resend_hash':'old-challenge'}])
        self.assertEqual(load_login_challenge(self.root),'new-challenge')
        self.assertFalse(await module._login_if_requested(self.root,cfg,self.adapter));self.assertEqual(len(seen),1)
    async def test_private_failure_frames_do_not_include_exception_or_source(self):
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('safe_frames',ROOT/'scripts/companion.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        async def fail(*a):raise ValueError('secret-login-example')
        # main owns asyncio.run, so execute it outside this test's running loop.
        def invoke():
            with patch.dict('os.environ',{'OUROBOROS_SKILL_STATE_DIR':str(self.root)}),patch.object(module,'_serve',fail):
                with self.assertRaises(SystemExit):module.main()
        await asyncio.to_thread(invoke)
        raw=(self.root/'companion-last-error.json').read_text()
        self.assertNotIn('secret-login-example',raw)
        self.assertIn('frames',raw)
        self.assertTrue(any(frame['function']=='fail' for frame in json.loads(raw)['frames']))

    async def test_resend_actual_tl_constructor_and_safe_metadata(self):
        old=self.client.__class__.__call__
        async def call(c,r):
            self.assertIsInstance(r,F.auth.ResendCodeRequest);bytes(r)
            return NS(phone_code_hash='not-a-code',type=T.auth.SentCodeTypeApp(5),next_type=T.auth.CodeTypeSms(),timeout=60)
        self.client.__class__.__call__=call
        try:
            self.assertEqual(await self.adapter.request_login_code('+1234567890',resend_hash='challenge'),'not-a-code')
            self.assertEqual(self.adapter.code_delivery,{'type':'SentCodeTypeApp','next_type':'CodeTypeSms','timeout':60})
        finally:self.client.__class__.__call__=old

if __name__=='__main__':unittest.main()
