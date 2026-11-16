"""Synthetic provider recorders used only by isolated_authority_probe's throwaway extension."""
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import sys


def install(plugin, api, payload):
    sys.path[:0] = [str(payload), str(payload / 'tests')]
    from lib.client import MailClient
    from lib.gmail import GmailClient
    from lib.ews import EwsClient
    from lib.store import EmailStore
    from test_intake_identity_equivalence import IdentityExchange, RAW, metadata
    from test_mutation_identity_authority import SourceBox
    from test_ews_mailbox import ACCOUNT
    state = Path(api.get_state_dir())
    def record(effect):
        with (state / 'identity-effects.jsonl').open('a') as stream:
            stream.write(json.dumps(effect) + '\n')
    original = plugin.MailService.target
    @contextmanager
    def target(self, account_id=''):
        if not account_id.startswith('identity:'):
            with original(self, account_id) as value:
                yield value
            return
        provider = account_id.split(':')[1]
        if provider == 'imap':
            box = SourceBox(RAW['obsolete']); uid = box.uid
            def command(*args):
                result = uid(*args)
                if args[0] != 'FETCH': record([provider, *args])
                return result
            box.uid = command
            client = MailClient({}); client.imap = box.open
        elif provider == 'gmail':
            client = GmailClient.__new__(GmailClient); client.account = {'id': account_id, 'provider':'gmail'}
            def request(method, path, **kw):
                if method != 'GET':
                    record([provider, method, path, kw.get('body')]); return {}
                if path == '/labels': return {'labels':[{'id':'Label_1','name':'Public'}]}
                native = 'd1' if path in ('/messages/d1','/drafts/draft-one') else '7b'
                msg = {'id':native, 'labelIds':['DRAFT'] if native == 'd1' else ['INBOX','UNREAD'],
                       'payload':{'headers':[{'name':k,'value':v} for k,v in metadata('obsolete').items()]}}
                if kw.get('params',{}).get('format') == 'raw':
                    msg['raw'] = base64.urlsafe_b64encode(RAW['obsolete']).decode()
                return {'id':'draft-one','message':msg} if path.startswith('/drafts/') else msg
            client._request = request
        else:
            server = IdentityExchange('encoded_reply'); post = server.post
            def request(*args, **kwargs):
                result = post(*args, **kwargs)
                op = server.operations[-1]
                name = op.tag.split('}')[-1]
                if name in ('UpdateItem','MoveItem','CopyItem'): record([provider,name])
                return result
            server.post = request
            client = EwsClient({**ACCOUNT,'address':'b@example.invalid'},session=server)
        yield {'id':account_id, 'provider':provider, 'address':'owner@example.invalid',
               'senders':['owner@example.invalid']}, EmailStore(state / provider), client
    plugin.MailService.target = target


def exercise(invoke, places, drive, effects):
    from dataclasses import replace
    from lib.mime import parse_message
    from test_intake_identity_equivalence import RAW
    from test_mutation_identity_authority import GMAIL, IMAP, EWS, ROOT_OF
    from test_ews_mailbox import ACCOUNT
    from test_ews_ownership import legacy_message
    for provider, actions in [('imap',IMAP), ('gmail',GMAIL), ('ews',EWS)]:
        account = 'identity:'+provider
        root = '<root@example.invalid>' if provider == 'ews' else ROOT_OF(parse_message(RAW['obsolete'],folder='',uid=0))
        places.connect_place(drive,connector='email-presence',provider='email',account_id=account,state='active')
        held = places.connect_place(drive,connector='email-presence',provider='email',account_id=account,
                                     conversation_id=root,state='paused')
        places._write(drive,held.place_id,lambda row:replace(row,thread_id=root))
        for action, case in actions.items():
            tool, selection = case[:2]
            args = {'account_id':account, **selection}
            if provider == 'ews':
                args['message_ref'] = legacy_message({'id':account}, 'B-msg')
            elif action != 'delete_draft':
                args.update(uid=4 if provider == 'imap' else 123,uidvalidity=7 if provider == 'imap' else 1)
                if provider == 'imap': args['folder']='Legal'
            before = effects.read_text() if effects.exists() else ''
            prepared = json.loads(invoke(tool,args)); retry = prepared['retry_arguments']
            assert retry['message_thread_id'] == root
            assert 'Paused' in invoke(tool,retry)
            assert (effects.read_text() if effects.exists() else '') == before
            places.edit_place(drive,held.place_id,'set_state',author='owner',state='active')
            result = json.loads(invoke(tool,retry)); assert result.get('ok',True)
            after = effects.read_text().splitlines()
            assert len(after) == len(before.splitlines()) + 1
            effect = json.loads(after[-1])
            assert effect[0] == provider
            if provider == 'imap': assert effect[1:] == list(case[2])
            if provider == 'ews': assert effect[1:] == [case[2]]
            if provider == 'gmail':
                expected = list(case[3])
                # This recorder has both INBOX and UNREAD; actions preserve the caller's exact modification.
                assert effect[1:] == expected
            places.edit_place(drive,held.place_id,'set_state',author='owner',state='paused')
    print('all provider mutation paths crossed the real child boundary with zero held effects',flush=True)
