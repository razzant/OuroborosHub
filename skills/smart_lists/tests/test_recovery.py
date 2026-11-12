"""Regressions for the four kanban review findings, via registered consumers."""
import json
from test_smart_lists import SkillCase


class RecoveryTests(SkillCase):
    def setUp(self):
        super().setUp()
        self.ok('group', action='create', name='Work')

    def create(self, text='task', **fields):
        return self.ok('add', group='Work', items=[{'text': text, **fields}], request_id='add-'+text)['added'][0]['id']

    def test_legacy_open_preserves_intermediate_stages(self):
        for stage in ('review', 'in_progress'):
            eid = self.create(stage, stage=stage)
            result = self.api.call_route('edit', 'POST', {'entry_id': eid, 'text': stage+' edited', 'status': 'open'})
            self.assertTrue(result['ok'])
            self.assertEqual(self.api.call_route('entry', 'GET', query={'entry_id': eid})['entry']['stage'], stage)
            self.ok('complete', entry_ids=[eid], done=False)
            self.assertEqual(self.api.call_route('entry', 'GET', query={'entry_id': eid})['entry']['stage'], stage)
            self.ok('complete', entry_ids=[eid], done=True)
            self.ok('complete', entry_ids=[eid], done=False)
            self.assertEqual(self.api.call_route('entry', 'GET', query={'entry_id': eid})['entry']['stage'], 'todo')

    def test_full_card_replays_keep_defaults_and_current_fields(self):
        for index, details in enumerate(({}, {'tags': ['one']})):
            eid = self.create(str(index), **({'details': details} if details else {}))
            for route, body in (
                ('edit', {'entry_id': eid, 'changes': {'description': 'context'}, 'expected': {'description': ''}, 'request_id': 'edit-'+str(index)}),
                ('archive', {'entry_id': eid, 'archived': True, 'expected': {'archived_at': None}, 'request_id': 'archive-'+str(index)}),
            ):
                first = self.api.call_route(route, 'POST', body)
                self.assertTrue(first['ok'], first)
                before = self.store_bytes()
                replay = self.api.call_route(route, 'POST', body)
                self.assertTrue(replay['replayed'])
                self.assertEqual(first['entry'], replay['entry'])
                self.assertEqual(before, self.store_bytes())
            self.ok('update', entry_id=eid, details={'note': 'later'})
            replay = self.api.call_route('edit', 'POST', {'entry_id': eid, 'changes': {'description': 'context'}, 'expected': {'description': ''}, 'request_id': 'edit-'+str(index)})
            self.assertEqual(replay['entry']['details']['note'], 'later')
            self.ok('delete', entry_ids=[eid])
            self.ok('delete', entry_ids=[eid], action='erase')
            replay = self.api.call_route('edit', 'POST', {'entry_id': eid, 'changes': {'description': 'context'}, 'expected': {'description': ''}, 'request_id': 'edit-'+str(index)})
            self.assertEqual(replay['entry'], {'id': eid, 'gone': True})

    def test_legacy_mutation_replay_shape(self):
        for index, details in enumerate(({}, {'tags': ['one']})):
            eid = self.create('legacy-'+str(index), **({'details': details} if details else {}))
            args = {'entry_id': eid, 'text': 'updated', 'request_id': 'legacy-update-'+str(index)}
            first = self.ok('update', **args)
            replay = self.ok('update', **args)
            self.assertTrue(replay['replayed'])
            self.assertEqual(first['entry'], replay['entry'])
            service = self.module._service()
            first = service.edit(eid, text='edited', status='open', request_id='legacy-edit-'+str(index))
            replay = service.edit(eid, text='edited', status='open', request_id='legacy-edit-'+str(index))
            self.assertTrue(replay['replayed'])
            self.assertEqual(first['entry'], replay['entry'])

    def test_archive_excluded_before_legacy_counts_and_pages(self):
        for i in range(102):
            eid = self.create('archived-'+str(i), stage='review' if i % 2 else 'done')
            self.ok('archive', entry_id=eid, archived=True, expected={'archived_at': None}, request_id='arc-'+str(i))
        active = self.create('active', stage='in_progress')
        selection = self.ok('select', group='Work', limit=1)
        self.assertEqual(selection['total'], 1)
        self.assertEqual(selection['entries'][0]['entry_id'], active)
        self.assertEqual(selection['entries'][0]['stage'], 'in_progress')
        self.assertIsNone(selection['entries'][0]['archived_at'])
        today = self.api.call_route('today', 'GET', query={'timezone': 'UTC'})
        self.assertEqual(today['total'], 1)
        self.assertEqual([row['id'] for row in today['rows']], [active])
        self.assertEqual(today['archive_total'], 0)
        view = self.api.call_route('view', 'GET')
        self.assertEqual(view['stats']['open'], 1)
        self.assertEqual(view['stats']['done'], 0)
        self.assertEqual(view['tree_rows'][0]['open'], 1)
        self.assertEqual(view['open_rows'][0]['stage'], 'in_progress')
        selected = self.api.call_route('select', 'GET', query={'group': 'Work'})
        self.assertEqual(selected['rows'][0]['stage'], 'in_progress')
        self.assertEqual(self.ok('read', status='all', archive='archived', limit=1)['total'], 102)

    def test_short_reference_summaries_stay_bounded_and_lossless_pages(self):
        for index, unit in enumerate(('x', '😀', '\\"')):
            refs = [{'label': (unit * 120)[:120], 'target': 'https://example.org/' + (unit * 100)[:100]} for _ in range(100)]
            args = {'group': 'Work', 'items': [{'text': 'large-'+str(index), 'details': {'references': refs}}], 'request_id': 'large-'+str(index)}
            def raw(name, **kw):
                result = self.api.tools[name]['handler'](None, **kw)
                self.assertLessEqual(len(result.encode('utf-8')), self.module.MAX_TOOL_RESULT_BYTES)
                parsed = json.loads(result)
                self.assertTrue(parsed['ok'], parsed)
                self.assertTrue(parsed['output_truncated'])
                return parsed
            result = raw('add', **args)
            eid = result['added'][0]['id']
            count = result['result_counts']['added[0].details.references']
            self.assertEqual(count['total'], 100)
            self.assertEqual(count['shown'], len(result['added'][0]['details']['references']))
            self.assertTrue(count['truncated'])
            self.assertIn('full_entry_reader', result)
            raw('add', **args)
            raw('read', limit=1, query='large-'+str(index))
            raw('update', entry_id=eid, details={'references': refs}, request_id='update-'+str(index))
            raw('update', entry_id=eid, details={'references': refs}, request_id='update-'+str(index))
            self.assertEqual(self.api.call_route('entry', 'GET', query={'entry_id': eid})['entry']['details']['references'], refs)
            collected, offset = [], 0
            while offset < len(refs):
                page = self.ok('entry', entry_id=eid, field='references', offset=offset)
                collected += page['value']
                offset = page['next_offset']
            self.assertEqual(collected, refs)
