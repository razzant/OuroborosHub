"""QA-only witness: 2.1 and 2.5 declarations both read a transfer in a real plugin child."""
import json

import pytest

from ouroboros import extension_loader
from ouroboros.chronicle_store import ChronicleStore
from ouroboros.contracts.plugin_api import PLUGIN_API_VERSION
from tests._extension_loader_shared import _clear_loader_state, _add_fake_native_dep  # noqa: F401
from tests.test_memory_reader_plugin import ATLAS, _load, _dispatch


@pytest.mark.parametrize('declared', ['2.1', '2.5'])
def test_transfer_through_plugin_child(tmp_path, monkeypatch, declared):
    assert PLUGIN_API_VERSION == '2.5'
    repo, data = _load(tmp_path, 'transfer_child', ATLAS,
                       f'plugin_api: "{declared}"\ndependencies:\n  - dummy_pkg\n', isolated=True)
    monkeypatch.setattr('ouroboros.config.get_skills_repo_path', lambda: str(repo))
    route = extension_loader.list_routes()['/api/extensions/transfer_child/atlas/read']
    assert route['out_of_process'] is True
    store = ChronicleStore(data)
    assert store.ensure_activated()['kind'] == 'activation'
    author = {'kind': 'source_author', 'label': 'Synthetic Writer', 'model': None, 'basis': 'importer'}
    transfer = {'operator': None, 'tool': 'synthetic-import 1.0', 'task_id': None}
    span = {'start': '2026-07-01', 'end': None, 'incomplete': True}
    words = 'Synthetic transferred words.\n' * 400
    assert store.publish([{'id': 'qa-transfer', 'kind': 'legacy', 'room_id': 'legacy',
                          'text': words, 'author': author,
                          'metadata': {'transfer': transfer, 'stated_period': span,
                                       'legacy_written_at': '2026-08-10'}}]).ok

    def read(op, params):
        status, body = _dispatch(data, 'transfer_child', 'atlas/read',
                                 {'op': op, 'params': json.dumps(params)})
        assert status == 200, body
        return body['answer']

    document = read('document', {'address': {'kind': 'record', 'id': 'qa-transfer'}, 'length': 20000})
    assert document['text'] == words
    assert document['stored']['author'] == author
    assert document['record']['transfer'] == transfer
    assert document['record']['stated_period'] == span
    assert document['record']['written_at'] == '2026-08-10'
    assert document['period'] == '2026-07-01 00:00 → ? (incomplete) (stated by its source)'
    story = read('catalog', {'area': 'story'})['entries']
    entry = next(e for e in story if e['id'] == 'qa-transfer')
    assert (entry['author'], entry['transfer'], entry['stated_period'], entry['period_basis']) == (
        author, transfer, span, 'stated')
