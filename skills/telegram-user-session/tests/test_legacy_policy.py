"""Actual legacy queues/adapters with current policy; no network or session access."""
import asyncio,hashlib,json,sys,tempfile,unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from test_participant import Client,Adapter,HostFake
from userbot.config import Config,save_config
from userbot.participant import CurrentAuthorization
from userbot.participant_state import save_policy
from userbot.runtime import UserSessionBridge,_atomic_json
from userbot.store import DurableStore
from userbot.action_store import ActionStore
from telethon import types as T
ACCOUNT=8667525238;PEER=-1000000000123;BINDING='b'*32
class LegacyClient(Client):
    def __init__(self):super().__init__();self.target_reads=0;self.before_target=None
    async def __call__(self,request):
        if type(request).__name__=='GetMessagesRequest':
            bytes(request);self.target_reads+=1
            if self.before_target:self.before_target(self.target_reads)
            return NS(messages=[T.Message(id=10,peer_id=T.PeerChannel(123),date=None,message='hello',out=True,from_id=T.PeerUser(ACCOUNT))])
        return await super().__call__(request)
class LegacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.stack=ExitStack()
        self.cfg=Config(123,'a'*32,'+1234567890',account_id=ACCOUNT,binding_id=BINDING,allowed_peer_ids=(PEER,))
        save_config(self.root,self.cfg);save_policy(self.root,{'mode':'legacy','outgoing':True,'expected_account_id':str(ACCOUNT)})
        self.client=LegacyClient();self.adapter=Adapter(self.root/'unused.session',123,'a'*32,client=self.client)
        async def no_forum(_peer):return {1}  # Legacy Actions supports a verified forum's General topic.
        self.adapter.topic_ids=no_forum
        async def target(peer,ids=None,**kwargs):
            return NS(id=ids,peer_id=T.PeerChannel(123),from_id=T.PeerUser(ACCOUNT),out=True,media=None,entities=[],reply_to=None)
        self.client.get_messages=target
        self.store=self.stack.enter_context(DurableStore(self.root/'transport.sqlite3',recover_attempted=True))
        self.actions=self.stack.enter_context(ActionStore(self.root/'actions.sqlite3',recover_attempted=True))
        self.host=HostFake();self.host.state='active'
        auth=CurrentAuthorization(self.root,self.host,ACCOUNT,'legacy')
        self.bridge=UserSessionBridge(self.root,self.cfg,self.adapter,self.store,NS(),action_store=self.actions,authorization=auth)
    def tearDown(self):self.stack.close();self.tmp.cleanup()
    def queue_text(self,name='send'):
        _atomic_json(self.root/'outgoing'/(name+'.json'),{'peer_id':PEER,'topic_id':None,'reply_id':None,'binding_id':BINDING,'text':'hello'})
        return self.store.enqueue(str(ACCOUNT),name,peer_id=PEER,payload_ref=name)
    def queue_action(self,op,name):
        kw={}
        if op!='delete':
            payload={'binding_id':BINDING,'operation':op,'peer_id':PEER,'topic_id':None,'message_id':10, 'text':'changed','emoji':'x'}
            _atomic_json(self.root/'actions'/(name+'.json'),payload)
            kw={'payload_ref':name,'payload_sha256':hashlib.sha256((self.root/'actions'/(name+'.json')).read_bytes()).hexdigest()}
        return self.actions.enqueue(str(ACCOUNT),name,operation=op,peer_id=PEER,binding_id=BINDING,topic_id=None,message_id=10,**kw)
    async def test_active_legacy_text_and_all_three_actions_still_dispatch(self):
        self.queue_text();await self.bridge.send_one();self.assertEqual(self.store.get(str(ACCOUNT),'send').status,'sent')
        for op in ('edit','delete','react'):
            self.queue_action(op,op);await self.bridge.process_action_once()
            self.assertEqual(self.actions.get(str(ACCOUNT),op).status,'sent')
        self.assertEqual(len(self.client.calls),4)
    async def test_revoked_outgoing_and_silent_paused_stop_queued_text(self):
        for index,state in enumerate(('off','silent','paused')):
            self.host.state='active' if state=='off' else state
            save_policy(self.root,{'outgoing':state!='off'})
            name='send'+str(index);self.queue_text(name);await self.bridge.send_one()
            self.assertEqual(self.store.get(str(ACCOUNT),name).status,'failed')
        self.assertEqual(self.client.calls,[])
    async def test_binding_change_during_host_await_is_rechecked(self):
        self.queue_text()
        def changing(*_):
            save_config(self.root,replace(self.cfg,binding_id='c'*32))
            return {'state':'active'}
        self.host.place=changing;await self.bridge.send_one()
        self.assertEqual(self.client.calls,[]);self.assertEqual(self.store.get(str(ACCOUNT),'send').status,'failed')
    async def test_text_final_guard_after_entity_await_prevents_rpc(self):
        self.queue_text();old=self.client.get_input_entity
        async def changing(peer):
            save_policy(self.root,{'outgoing':False});return await old(peer)
        self.client.get_input_entity=changing;await self.bridge.send_one()
        self.assertEqual(self.client.calls,[]);self.assertNotEqual(self.store.get(str(ACCOUNT),'send').status,'sent')
    async def test_action_final_guard_after_repeated_target_preflight(self):
        self.queue_action('delete','delete')
        def changing(reads):
            if reads==2:self.host.state='paused'
        self.client.before_target=changing;await self.bridge.process_action_once()
        self.assertEqual(self.client.target_reads,2);self.assertEqual(self.client.calls,[])
        self.assertEqual(self.actions.get(str(ACCOUNT),'delete').status,'unknown')
        await self.bridge.process_action_once();self.assertEqual(self.client.calls,[])
    async def test_attempted_text_remains_unknown_without_replay(self):
        self.queue_text();self.client.raise_error=TimeoutError('synthetic transport')
        await self.bridge.send_one();self.assertEqual(len(self.client.calls),1)
        self.store.close()
        recovered=self.stack.enter_context(DurableStore(self.root/'transport.sqlite3',recover_attempted=True))
        self.bridge.store=recovered;await self.bridge.send_one()
        self.assertEqual(len(self.client.calls),1);self.assertEqual(recovered.get(str(ACCOUNT),'send').status,'unknown')
if __name__=='__main__':unittest.main()
