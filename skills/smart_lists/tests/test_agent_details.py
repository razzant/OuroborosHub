"""Agent capture uses the same registered surfaces as ordinary lists."""
import asyncio
import copy
import json
from unittest import mock
from starlette.datastructures import QueryParams
from test_smart_lists import SkillCase, FakeRequest


class AgentDetailsTests(SkillCase):
    def setUp(self):
        super().setUp()
        self.ok('group', action='create', name='Work')

    def capture(self, key='thread:one:decision', request='capture-1', **details):
        return self.tool('add', group='Work', request_id=request, items=[{
            'text': 'Проверить результат', 'details': {
                'capture_key': key, 'tags': ['Nova', 'review'], 'turn': 'owner',
                'next_step': 'Ответить в исходном треде',
                'references': [{'label': 'Исходный запрос', 'target': 'https://example.org/thread/1'},
                               {'label': 'Работа', 'target': 'task:abc'}], **details}}])

    def test_capture_update_filter_replay_and_export(self):
        result=self.capture()
        self.assertTrue(result['ok'], result)
        entry=result['added'][0]
        replay=self.capture()
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['added'][0]['details'], entry['details'])
        self.ok('update', entry_id=entry['id'], details={'turn':'agent', 'note':'Owner answered', 'tags':['Nova','nova','FIX']})
        shown=self.ok('read', tags=['nova','fix'], turn='agent', query='answered')['entries']
        self.assertEqual(len(shown),1)
        self.assertEqual(shown[0]['details']['tags'],['Nova','FIX'])
        self.assertEqual(len(shown[0]['details']['references']),2)
        self.assertEqual(self.ok('read', tags=['missing'],tag_mode='any')['total'],0)
        doc=json.loads(self.store_bytes())
        self.assertNotIn('details', json.dumps(doc['requests']))
        exported=self.ok('store',action='export')
        fresh=self.fresh_api(self.state_dir.parent/'restored')
        restored=fresh.call_tool('store',action='restore',file=exported['file'])
        self.assertTrue(restored['ok'],restored)
        self.assertEqual(fresh.call_tool('read',status='all')['entries'],self.api.call_tool('read',status='all')['entries'])

    def test_capture_key_covers_done_and_trash_without_resurrection(self):
        entry=self.capture()['added'][0]
        self.ok('complete',entry_ids=[entry['id']])
        duplicate=self.capture(request='capture-2')
        self.assertFalse(duplicate['ok'])
        self.assertEqual(duplicate['error']['code'],'capture_exists')
        self.assertIn(entry['id'],duplicate['error']['message'])
        self.ok('delete',entry_ids=[entry['id']])
        self.assertEqual(self.capture(request='capture-3')['error']['code'],'capture_exists')
        self.assertEqual(self.ok('read',status='deleted')['total'],1)
        self.assertEqual(self.ok('read')['total'],0)
        self.assertTrue(self.capture(key='thread:one:another-obligation',request='capture-4')['ok'])

    def test_schema3_read_only_upgrade_preserves_bloom_and_records(self):
        self.add('Work','Old text')
        doc=json.loads(self.store_bytes());doc['schema_version']=3
        core=self.module.lists_core
        bits = core._bloom_decode(doc['retired_requests'])
        core._bloom_add(bits, core._request_digest('old-request'))
        doc['retired_requests'] = core._bloom_encode(bits)
        before=copy.deepcopy(doc)
        self.state_dir.joinpath('store.json').write_text(json.dumps(doc))
        self.mark_fixture_as_legacy_store()
        raw=self.store_bytes()
        self.ok('read')
        self.assertEqual(raw,self.store_bytes())
        eid=next(iter(doc['entries']))
        self.ok('update',entry_id=eid,details={'turn':'owner'})
        after=json.loads(self.store_bytes())
        self.assertEqual(after['schema_version'],4)
        self.assertEqual(after['retired_requests'],before['retired_requests'])
        self.assertEqual(after['groups'],before['groups'])
        self.assertEqual(after['entries'][eid]['text'],'Old text')
        self.refused('add','request_expired',group='Work',items=[{'text':'other'}],request_id='old-request')

    def test_invalid_metadata_atomic_and_immutable_key(self):
        for detail in [{'turn':'done'},{'tags':['']},{'references':[{'label':'bad','target':'javascript:alert(1)'}]},
                       {'references':[{'label':'bad','target':'https://name:secret@example.org'}]},
                       {'note':'a'*1001},{'tags':['\ud800']},{'capture_key':123}]:
            before=self.store_bytes()
            result=self.capture(**detail)
            self.assertFalse(result['ok'],result)
            self.assertEqual(before,self.store_bytes())
        entry=self.capture()['added'][0]
        self.refused('update','invalid_input',entry_id=entry['id'],details={'capture_key':'different'})

    def test_board_filters_before_pagination_and_is_read_only(self):
        for batch in range(2):
            self.ok('add',group='Work',request_id=f'batch-{batch}',items=[{'text':f'item{batch}-{n}','details':{'tags':['keep' if n%2 else 'skip'],'turn':'owner'}} for n in range(100)])
        request=FakeRequest(); request.query_params=QueryParams('tag=keep&turn=owner&offset=0')
        before=self.store_bytes()
        result=asyncio.run(self.api.routes['board']['handler'](request))
        self.assertTrue(result['ok'],result)
        self.assertEqual((len(result['entries']),result['total']),(100,100))
        self.assertEqual(result['revision']['generation'],json.loads(before)['generation'])
        self.assertEqual(before,self.store_bytes())
        self.assertEqual(result['available_tags'],['keep','skip'])
        self.assertEqual(result['filter_groups'][0]['name'],'Work')

    def test_merge_total_size_validation(self):
        entry=self.capture(note='n'*1000,next_step='x'*1000)['added'][0]
        before=self.store_bytes()
        self.refused('update','invalid_input',entry_id=entry['id'],details={'references':[{'label':'L','target':'https://example.org/'+('x'*950)}]*3})
        self.assertEqual(before,self.store_bytes())

    def test_trash_only_tag_available_beyond_first_page(self):
        for batch in range(2):
            self.ok('add',group='Work',request_id=f'trash-{batch}',items=[{'text':f'item-{batch}-{i}'} for i in range(60)])
        entry=self.capture(tags=['trash-only'])['added'][0]
        self.ok('delete',entry_ids=[entry['id']])
        board=self.module.widget_board(status='deleted')
        self.assertIn('trash-only',board['available_tags'])
        self.assertEqual(board['entries'][0]['id'],entry['id'])

    def test_schema3_matching_sentinel_full_records_survive(self):
        import hashlib
        ids=self.add('Work','open','done','trash')
        self.ok('complete',entry_ids=[ids[1]])
        self.ok('delete',entry_ids=[ids[2]])
        doc=json.loads(self.store_bytes());doc['schema_version']=3
        raw=json.dumps(doc,ensure_ascii=False,indent=1).encode()
        self.state_dir.joinpath('store.json').write_bytes(raw)
        meta_path=self.state_dir/'store_meta.json';meta=json.loads(meta_path.read_text())
        meta['content_sha256']=hashlib.sha256(raw).hexdigest();meta_path.write_text(json.dumps(meta))
        self.ok('read',status='all');self.assertEqual(raw,self.store_bytes())
        self.ok('group',action='create',name='Another')
        after=json.loads(self.store_bytes())
        self.assertEqual(after['entries'],doc['entries'])
        self.assertEqual(after['requests'],doc['requests'])
        self.assertEqual(after['retired_requests'],doc['retired_requests'])
