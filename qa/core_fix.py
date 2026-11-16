"""Addressed cloud-only core delta; reuses qualify's custody, collection and export.

No imports from the product. Missing reviewed pins or historical bytes stop prepare.
"""
import json
import os
import shutil
import sys

BEFORE = 'e6c85292beed8dce43abac5c6fe4002b6f57aec9'
ISO = 'tests/test_memory_view_iso_period.py'
DIFFERENTIAL = ISO + '::test_port_matches_native_312_on_a_generated_corpus'
FALLBACK = [f'tests/test_memory_fallback{suffix}.py' for suffix in ('', '_sources', '_reachability', '_max')]
STORY = ['tests/test_common_story.py', 'tests/test_memory_view_story.py',
         'tests/test_memory_story_labels.py', 'tests/test_legacy_memory_notice.py',
         'tests/test_chronicle_import.py']
COUNT_LABELS = ('core-fix-iso', 'core-fix-capacity', 'core-fix-inventory',
                'core-fix-transfer', 'core-fix-story', 'core-fix-fallback', 'core-fix-size',
                'core-fix-atlas-mixed', 'core-fix-catalog')


def inventory(q, root):
    files = {}
    for path in sorted(root.rglob('*')):
        assert not path.is_symlink(), f'historical evidence must be ordinary files: {path}'
        if path.is_file():
            files[path.relative_to(root).as_posix()] = q.file_digest(path)
    return files


def validate(q):
    assert q.SELECTION == 'core-fix'
    assert q.PINS.get('state') == 'ready', 'parent must bind qa/core-fix-subjects.json after core review'
    spec = q.PINS['core_fix']
    assert spec['before_commit'] == BEFORE
    for value in [q.PINS[key] for key in ('core_commit', 'core_tree', 'target_commit', 'target_tree')] + [spec['before_tree']]:
        assert len(value) == 40 and all(c in '0123456789abcdef' for c in value)
    assert q.PINS['core_commit'] != BEFORE, 'before is not the repaired candidate'
    nodes = spec['before_nodes']
    assert nodes and len(nodes) == len(set(nodes))
    assert all(n.startswith(ISO + '::test_') and not any(c.isspace() for c in n) for n in nodes)
    assert nodes == [ISO + '::test_real_312_publication_replays_with_short_full_and_f3_periods'], 'this control must reach the actual node reader'
    assert spec['failure_marker'] == 'assert (first, end, next_start) == (0, total, None)'
    assert spec['history_files'], 'historical input cannot be empty'
    for label in COUNT_LABELS:
        count = spec['expected_counts'][label]
        assert type(count) is int and count > 0, (label, count)
        q.EXPECTED_COUNTS[label] = count
    assert DIFFERENTIAL not in nodes, 'the before control must exercise a reader, not a new helper'
    assert spec['fixture_commit'] == '42aab559faca79bac21bf7630a82ac9fb9667702'
    q.EXPECTED_COUNTS.update({'core-fix-before-n3': len(nodes), 'core-fix-before312': len(nodes),
                             'core-fix-iso312': q.EXPECTED_COUNTS['core-fix-iso'] + 1})
    assert q.EXPECTED_COUNTS['core-fix-iso'] >= len(nodes)


def prepare(q):
    spec = q.PINS['core_fix']
    before = q.TEMP / 'core-fix-before'
    assert q.git(q.CORE, 'rev-parse', BEFORE + '^{tree}') == spec['before_tree']
    assert q.command('clone-core-fix-before', ['git', 'clone', '--shared', '--no-checkout',
                     str(q.CORE), str(before)], q.TEMP) == 0
    assert q.command('read-core-fix-before', ['git', 'read-tree', BEFORE], before) == 0
    assert q.command('materialize-core-fix-before', ['git', 'checkout-index', '-a'], before) == 0
    assert q.git(before, 'write-tree') == spec['before_tree']
    for name in ('uv.lock', 'pyproject.toml', 'requirements-runtime.lock'):
        assert (before / name).read_bytes() == (q.COMPOSED / name).read_bytes(), name + ': requires a separate environment'
    q.snapshot(before, 'core-fix-before-source')
    source = q.COMPOSED / ISO
    assert q.file_digest(source) == spec['iso_test_sha256']
    assert (q.CORE / ISO).read_bytes() == source.read_bytes(), 'composition changed the home test'
    assert not (before / ISO).exists(), 'before overlay must not overwrite an existing test'
    shutil.copyfile(source, before / ISO)
    assert q.file_digest(before / ISO) == spec['iso_test_sha256']
    shutil.copyfile(source, q.OUT / 'core-fix-iso-source.py')
    q.save(q.OUT / 'core-fix-overlay.json', {'path': ISO, 'sha256': spec['iso_test_sha256'],
            'source_commit': q.PINS['core_commit'], 'before_commit': BEFORE})
    # Parent copies the already retained R5 N3 directory here; never regenerate it.
    history = q.QA / 'core-fix-history'
    files = inventory(q, history)
    assert files == spec['history_files'], 'historical input copy/hash mismatch'
    for name in ('writer.json', 'record.json', 'original-start.txt', 'state/memory/chronicle/records.jsonl'):
        assert name in files, 'missing historical input: ' + name
    writer = json.loads((history / 'writer.json').read_text())
    assert writer['python'].startswith('3.12.13 '), 'historical writer identity'
    assert files['state/memory/chronicle/records.jsonl'] == writer['journal_sha256']
    assert files['original-start.txt'] == writer['start']['sha256']
    # The old fixture drives the same synthetic input; runtime imports still use each selected core.
    assert files['fixture-source.py'] == writer['source_sha256'] == spec['fixture_source_sha256']
    assert q.command('history-fixture-git-source', ['git', 'show',
                     spec['fixture_commit'] + ':tests/test_chronicle_transfer.py'], q.CORE) == 0
    assert q.file_digest(q.OUT / 'history-fixture-git-source.stdout') == writer['source_sha256']
    assert files['c1-selected-setup.py'] == writer['selected_setup_sha256']
    for name, expected in spec['core_source_files'].items():
        assert q.file_digest(q.CORE / name) == q.file_digest(q.COMPOSED / name) == expected, name
    inputs = q.QA / 'core-fix-inputs'
    assert q.file_digest(inputs / '.github/workflows/catalog-check.yml') == spec['catalog_workflow_sha256']
    assert q.file_digest(inputs / 'tests/test_workflows.py') == spec['catalog_test_sha256']
    for name, expected in spec['hub_test_files'].items():
        assert q.file_digest(inputs / name) == q.file_digest(q.HUB / name) == expected, 'parent must integrate corrected Hub test: ' + name
    # Workflow tests read ROOT/.github/workflows/catalog-check.yml. Give them the actual
    # product workflow in a separate exact copy, not this temporary qualification workflow.
    catalog = q.TEMP / 'core-fix-catalog'
    shutil.copytree(q.HUB, catalog, ignore=shutil.ignore_patterns('.git', 'qa'))
    shutil.copyfile(inputs / '.github/workflows/catalog-check.yml', catalog / '.github/workflows/catalog-check.yml')
    q.save(q.OUT / 'core-fix-catalog-before.json', inventory(q, catalog))
    shutil.copyfile(inputs / '.github/workflows/catalog-check.yml', q.OUT / 'core-fix-product-workflow.yml')
    shutil.copytree(history, q.OUT / 'n3-history')
    assert inventory(q, q.OUT / 'n3-history') == files
    q.save(q.OUT / 'core-fix-ready.json', {'before_commit': BEFORE, 'before_tree': spec['before_tree'],
           'candidate_tree': q.git(q.COMPOSED, 'write-tree'), 'history_files': files,
           'expected_counts': {k: v for k, v in q.EXPECTED_COUNTS.items() if k.startswith('core-fix-')},
           'groups': {'iso310': [ISO, '--deselect=' + DIFFERENTIAL], 'iso312': [ISO],
                      'story': STORY, 'fallback': FALLBACK, 'before': spec['before_nodes']}})


def observe(q, label, core, version):
    output = q.OUT / 'n3' / label
    history = q.OUT / 'n3-history'
    shutil.copytree(history, output)
    assert inventory(q, output) == q.PINS['core_fix']['history_files']
    # Only the copied derived index is removed, to require journal replay. All CAS
    # bytes and the original retained bundle stay untouched, including its old index.
    excluded = []
    for name in ('index.sqlite3', 'index.sqlite3-wal', 'index.sqlite3-shm'):
        path = output / 'state/memory/chronicle' / name
        if path.exists():
            excluded.append({'path': str(path.relative_to(output)), 'sha256': q.file_digest(path)})
            path.unlink()
    q.save(q.OUT / f'{label}-copy.json', {'historical_files': inventory(q, history), 'derived_index_removed': excluded})
    q.execute(label, core, core, [str(q.QA / 'observe_n3.py'),
              'read312' if version == 312 else 'read-core310', str(core), str(output)])
    facts = json.loads((output / 'observation.json').read_text())
    assert facts['fit_status'] == 'UNVERIFIED'
    q.RESULTS[q.PLANNED.index(label)].update(status='OBSERVED', fit_status=facts['fit_status'])
    q.save(q.OUT / 'results.json', q.RESULTS)


def run(q):
    validate(q)
    assert (q.OUT / 'core-fix-ready.json').is_file(), 'core-fix preparation did not complete'
    q.runtime()
    writer = q.TEMP / 'python312/bin/python'
    assert writer.is_file(), 'the real 3.12 writer must be prepared before 3.10 tests'
    writer_env = {'OURO_TEST_TRANSFER_WRITER_PYTHON': str(writer)}
    for label, selection in (
        ('core-fix-iso', [ISO, '--deselect=' + DIFFERENTIAL]),
        ('core-fix-capacity', ['tests/test_memory_forms_capacity.py']),
        ('core-fix-inventory', ['tests/test_generated_inventories.py::test_data_layout_inventory_is_byte_identical']),
        ('core-fix-transfer', ['tests/test_chronicle_transfer.py']),
        ('core-fix-story', STORY),
        ('core-fix-fallback', FALLBACK),
        ('core-fix-size', ['-m', 'size_ratchet', 'tests/']),
    ):
        with q.family(label):
            env = writer_env if label == 'core-fix-iso' else {}
            if label == 'core-fix-size':
                env = {'OURO_SIZE_RATCHET_BASE_REF': q.PINS['target_commit']}
            q.pytest(label, q.COMPOSED, selection, env=env)
    spec = q.PINS['core_fix']
    before = q.TEMP / 'core-fix-before'
    with q.family('core-fix-atlas-mixed'):
        (q.OUT / 'visual' / 'core-fix').mkdir(parents=True, exist_ok=True)
        q.pytest('core-fix-atlas-mixed', q.COMPOSED,
                 ['tests/memory_atlas/test_widget_transfer.py::test_the_real_mixed_part_names_each_base_of_its_members'],
                 cwd=q.HUB, env={'ATLAS_CORE': str(q.COMPOSED), 'ATLAS_RUN_ROOT': '/tmp/mt-atlas-suite',
                                'ATLAS_SHOTS': str(q.OUT / 'visual' / 'core-fix'), 'PYTHONPATH': str(q.HUB)})
    with q.family('core-fix-catalog'):
        q.pytest('core-fix-catalog', q.COMPOSED, ['tests/test_workflows.py'],
                 cwd=q.TEMP / 'core-fix-catalog', env={'PYTHONPATH': str(q.TEMP / 'core-fix-catalog')})
    with q.family('core-fix-before-n3'):
        q.pytest('core-fix-before-n3', before, spec['before_nodes'], env=writer_env,
                 negative=([n.split('::')[-1] for n in spec['before_nodes']], spec['failure_marker']))
    for label, core in [('core-fix-observe-before', before), ('core-fix-observe-after', q.COMPOSED)]:
        with q.family(label):
            observe(q, label, core, 310)
    # A red family does not prevent the independent 3.12 comparison.
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write('comparison_ready=true\n')
    pending = {'core-fix-iso312', 'core-fix-before312', 'core-fix-observe312', 'core-fix-pair'}
    assert all(r.get('qualification_status') == 'PASS' for r in q.RESULTS if r['label'] not in pending)


def compare(q):
    validate(q)
    assert (q.OUT / 'core-fix-ready.json').is_file()
    assert sys.version_info[:3] == (3, 12, 13), sys.version
    q.save(q.OUT / 'runtime312.json', {'python': sys.version, 'executable': sys.executable,
           'prefix': sys.prefix, 'base_prefix': sys.base_prefix})
    with q.family('core-fix-iso312'):
        q.pytest('core-fix-iso312', q.COMPOSED, [ISO],
                 env={'OURO_TEST_TRANSFER_WRITER_PYTHON': sys.executable})
    with q.family('core-fix-before312'):
        q.pytest('core-fix-before312', q.TEMP / 'core-fix-before', q.PINS['core_fix']['before_nodes'],
                 env={'OURO_TEST_TRANSFER_WRITER_PYTHON': sys.executable},
                 negative=([n.split('::')[-1] for n in q.PINS['core_fix']['before_nodes']],
                           q.PINS['core_fix']['failure_marker']))
    with q.family('core-fix-observe312'):
        observe(q, 'core-fix-observe312', q.COMPOSED, 312)
    with q.family('core-fix-pair'):
        pair(q)
    assert all(r.get('qualification_status') == 'PASS' for r in q.RESULTS), 'core-fix is red; read results.json'


def pair(q):
    original = (q.OUT / 'n3-history/original-start.txt').read_text()
    rows = {}
    for label in ('core-fix-observe-before', 'core-fix-observe-after', 'core-fix-observe312'):
        assert q.RESULTS[q.PLANNED.index(label)].get('qualification_status') == 'PASS', label
        root = q.OUT / 'n3' / label
        rows[label] = json.loads((root / 'observation.json').read_text())
        for form in ('full', 'F3'):
            text = (root / (form + '.txt')).read_text()
            assert (original in text) == (label == 'core-fix-observe-before'), (label, form)
            assert rows[label][form]['sha256'] == q.file_digest(root / (form + '.txt'))
        assert rows[label]['original_start']['sha256'] == q.file_digest(q.OUT / 'n3-history/original-start.txt')
        record = json.loads((q.OUT / 'n3-history/record.json').read_text())
        assert rows[label]['stored_source_sha256'] == q.digest(record['text'].encode('utf-8'))
        assert rows[label]['fixture_source_sha256'] == q.PINS['core_fix']['fixture_source_sha256']
        if label != 'core-fix-observe-before':
            shown = q.PINS['core_fix']['historical_display']
            node = (root / 'node.txt').read_text()
            head = node.split('\n', 1)[0]
            assert f"; stated period {shown['start']} → {shown['end']}; " in head
            assert f"; written {shown['written']}; " in head
            assert '\ntext:\n' + record['text'] in node
            assert original not in node and original not in (root / 'fold.txt').read_text()
            assert len(head) < 2_000 and 'next_start=' not in node
            for form in ('full', 'fold'):
                rendered = (root / (form + '.txt')).read_text()
                assert shown['start'] + ' → ' + shown['end'] + ' (stated by its source)' in rendered
                assert 'written ' + shown['written'] in rendered
                assert record['text'] in rendered
    for form in ('full', 'F3', 'node', 'fold'):
        assert rows['core-fix-observe-after'][form]['utf8_bytes'] < rows['core-fix-observe-before'][form]['utf8_bytes']
        assert rows['core-fix-observe-after'][form] == rows['core-fix-observe312'][form]
    q.save(q.OUT / 'core-fix-pair.json', {'observations': rows, 'fit_status': 'UNVERIFIED',
           'before_control': 'same historical bytes and home-test overlay on e6c85292; no product mutation'})
    q.RESULTS[q.PLANNED.index('core-fix-pair')] = {'label': 'core-fix-pair', 'status': 'PASS',
                                                       'qualification_status': 'PASS', 'fit_status': 'UNVERIFIED'}
    q.save(q.OUT / 'results.json', q.RESULTS)


def audit(q):
    before = q.TEMP / 'core-fix-before'
    spec = q.PINS['core_fix']
    checks = {
        'before_source_unchanged': lambda: q.snapshot(before, 'core-fix-before-source-after') ==
            json.loads((q.OUT / 'core-fix-before-source.json').read_text()),
        'overlay_unchanged': lambda: q.file_digest(before / ISO) == spec['iso_test_sha256'],
        'history_unchanged': lambda: inventory(q, q.OUT / 'n3-history') == spec['history_files'],
        'published_history_unchanged': lambda: inventory(q, q.QA / 'core-fix-history') == spec['history_files'],
        'catalog_unchanged': lambda: inventory(q, q.TEMP / 'core-fix-catalog') ==
            json.loads((q.OUT / 'core-fix-catalog-before.json').read_text()),
    }
    facts, errors = {}, {}
    for name, check in checks.items():
        try:
            facts[name] = check()
        except (OSError, ValueError, KeyError, AssertionError) as exc:
            facts[name] = False
            errors[name] = f'{type(exc).__name__}: {exc}'
    q.save(q.OUT / 'core-fix-source-audit.json', {'checks': facts, 'errors': errors})
    return all(facts.values())
