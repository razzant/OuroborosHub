"""A bounded read keeps a small group tree before paginating its entries."""
from test_smart_lists import SkillCase


class ReadTreeBudgetTests(SkillCase):
    def test_large_registered_read_keeps_group_structure_and_pages_entries(self):
        groups = self.make_tree()
        self.add('Home / Groceries', *[f'Entry {i}: ' + 'x' * 100 for i in range(60)], request_id='tree-budget')
        result = self.ok('read', limit=100)
        self.assertEqual({g['id'] for g in result['groups']}, set(groups.values()))
        self.assertTrue(result['output_truncated'])
        self.assertGreater(result['next_offset'], 0)
        self.assertLess(result['next_offset'], 60)
        ids = {e['id'] for e in result['entries']}
        for _ in range(60):
            if result['next_offset'] >= result['total']:
                break
            offset = result['next_offset']
            result = self.ok('read', limit=100, offset=offset)
            self.assertGreater(result['next_offset'], offset)
            self.assertFalse(ids.intersection(e['id'] for e in result['entries']))
            ids.update(e['id'] for e in result['entries'])
        self.assertEqual(len(ids), 60)
