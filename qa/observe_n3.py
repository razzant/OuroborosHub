"""Cloud-only C1 historical writer (3.12) -> replay/full/F3 observer (3.10).

No new length threshold. A floor walk without a bound is not a context-fit PASS.
"""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

phase, core, output = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(core))
from ouroboros import memory_floor as mf, memory_view as mv  # noqa: E402
from ouroboros.chronicle_store import ChronicleStore  # noqa: E402

output.mkdir(parents=True, exist_ok=True)
root = output / 'state'
source = output / 'fixture-source.py' if (output / 'fixture-source.py').is_file() else core / 'tests/test_chronicle_transfer.py'
# A retained source supplies only the fixture helpers. Its imports resolve to the
# selected reader/runtime above; no history-writing function is called on read.
if phase != 'write':
    receipt = json.loads((output / 'writer.json').read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == receipt['source_sha256']
fixture = importlib.util.spec_from_file_location('n3_retained_fixture', source)
c1 = importlib.util.module_from_spec(fixture)
fixture.loader.exec_module(c1)
name = 'test_long_iso_period_is_bounded_in_every_api_reader_and_its_original_is_readable'


def save(name, data):
    (output / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def measure(name, text):
    raw = text.encode('utf-8')
    (output / f'{name}.txt').write_bytes(raw)
    return {'chars': len(text), 'utf8_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'json_chars': mv.json_chars(text), 'estimated_view_tokens': mf.view_tokens(text)}


if phase == 'write':
    assert sys.version_info[:3] == (3, 12, 13), sys.version
    assert not root.exists(), 'one fresh N3 writer only'
    root.mkdir()
    # Execute only C1's existing historical-journal setup, ending before its first
    # replay. Keep the exact selected source; no product test/assertion is changed.
    parsed = ast.parse(source.read_text())
    function = next(n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == name)
    stop = next(i for i, n in enumerate(function.body) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'replayed' for t in n.targets))
    function.body = function.body[:stop] + [ast.Return(value=ast.Name(id='record', ctx=ast.Load()))]
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    setup = ast.unparse(module)
    (output / 'c1-selected-setup.py').write_text(setup + '\n')
    namespace = vars(c1).copy()
    exec(compile(module, str(source), 'exec'), namespace)
    record = namespace[name](root)
    from datetime import datetime
    start = record['metadata']['stated_period']['start']
    parsed_start = datetime.fromisoformat(start)  # prove this writer accepts the preserved ISO
    save('record.json', record)
    save('writer.json', {'python': sys.version, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'fixture': name, 'selected_setup_sha256': hashlib.sha256((setup + '\n').encode()).hexdigest(),
        'journal_sha256': hashlib.sha256(ChronicleStore(root).log_path.read_bytes()).hexdigest(),
        'start': measure('original-start', start), 'parsed_start': parsed_start.isoformat(),
        'method': 'C1 historical transaction, not fresh publication'})
elif phase in ('read', 'read-core310', 'read312'):
    assert sys.version_info[:3] == ((3, 12, 13) if phase == 'read312' else (3, 10, 22)), sys.version
    writer = json.loads((output / 'writer.json').read_text())
    assert writer['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    record = json.loads((output / 'record.json').read_text())
    store = ChronicleStore(root)
    assert hashlib.sha256(store.log_path.read_bytes()).hexdigest() == writer['journal_sha256']
    assert not store.index_path.exists(), 'C1 writer must leave the index absent for cross-version replay'
    replayed = store.get(record['id'])
    assert {key: replayed[key] for key in record} == record
    if phase != 'read':
        source_text = c1._memory_read(c1._ctx(root), source_ref=replayed['source_refs'][0])
        assert record['text'] in source_text, 'retained original is not readable through its source reference'
        from ouroboros.artifacts import read_actor_source_bytes
        ref = replayed['source_refs'][0]
        original_bytes = read_actor_source_bytes(root, ref['task_id'], ref)
        assert original_bytes == record['text'].encode('utf-8'), 'historical CAS bytes changed'
        node_text = c1._memory_read(c1._ctx(root), node_id=record['id'])
        from ouroboros import memory_fallback
        fold = memory_fallback._part_input(root, store, memory_fallback.FallbackUnit(
            'part', '1', 'part:1:probe', member_ids=(record['id'],),
            head_sequence=store.room_head('1')), None, len).prompt
    save('replayed.json', replayed)
    snapshot = c1._view(root)
    (output / 'snapshot.json').write_text(mv.snapshot_json(snapshot))
    full = mv.render_story(snapshot)
    folded = mv.render_story(snapshot, mv.FloorLevel((('F3', ('1',)),)))
    assert record['id'] in full and record['id'] in folded, 'observation lost the C1 record'
    facts = {'python': sys.version, 'replay_equal': True, 'full': measure('full', full),
             'F3': measure('F3', folded), 'fit_status': 'UNVERIFIED',
             'fit_reason': 'No source-bound model window, reply reserve or complete request envelope supplied. '
                           'An unbounded floor walk cannot establish physical fit.',
             'fit_path': 'ouroboros.memory_floor.fit_memory_view',
             'allowances': {'margin': None, 'budget': None, 'physical': None}}
    # Exercise the existing unbounded path, retaining its result without inventing a limit.
    level = mf.fit_memory_view(snapshot, facts['allowances'])
    facts['unbounded_floor'] = {'addressed': level.addressed,
        'story': measure('unbounded-floor-story', mv.render_story(snapshot, level))}
    facts['F3_elements'] = [dict(step=s, id=i, full_chars=len(a), short_chars=len(b),
                               full_json_chars=mv.json_chars(a), short_json_chars=mv.json_chars(b))
                            for s, i, a, b in mf.floor_elements(snapshot) if s == 'F3']
    assert ChronicleStore(root).get(record['id'])['metadata'] == record['metadata']
    if phase != 'read':
        facts['original_start'] = measure('original-start', record['metadata']['stated_period']['start'])
        facts['original_source_read'] = measure('original-source-read', source_text)
        facts['stored_source_sha256'] = hashlib.sha256(original_bytes).hexdigest()
        facts['node'] = measure('node', node_text)
        facts['fold'] = measure('fold', fold)
        facts['fixture_source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        facts['reader_module_sha256'] = hashlib.sha256((core / 'ouroboros/tools/chronicle.py').read_bytes()).hexdigest()
        assert hashlib.sha256(store.log_path.read_bytes()).hexdigest() == writer['journal_sha256']
    save('observation.json', facts)
else:
    raise ValueError(phase)
