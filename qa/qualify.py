"""One disposable Actions job; stdlib orchestration, no product imports here.

prepare: bind the published source commits and materialize Git's clean merge tree.
run: unmodified home suites plus three known negative witnesses and UI capture.
export: retain raw results and exact synthetic roots on success or failure.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import traceback
import xml.etree.ElementTree as ET

QA = Path(__file__).resolve().parent
HUB = QA.parent
CORE = Path(os.environ['GITHUB_WORKSPACE']).resolve()
TEMP = Path(os.environ['RUNNER_TEMP']).resolve()
OUT = TEMP / 'evidence'
COMPOSED = TEMP / 'core-composed'
PINS = json.loads((QA / 'subjects.json').read_text())
OUT.mkdir(parents=True, exist_ok=True)
PLANNED = ['frozen-transfer', 'frozen-owners', 'composed-transfer', 'composed-owners',
           'size', 'plugin-compat', 'atlas', 'frames', 'negative-c1', 'negative-c3', 'negative-f3', 'datetime312']
EXPECTED_COUNTS = {'frozen-transfer': 80, 'composed-transfer': 80, 'size': 7,
                   'plugin-compat': 2, 'datetime312': 5}


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root).decode().strip()


def command(label, argv, cwd):
    """Raw streams and argv survive nonzero exits. Never interpret exit 1 as proof by itself."""
    save(OUT / f'{label}.command.json', {'argv': list(map(str, argv)), 'cwd': str(cwd)})
    with (OUT / f'{label}.stdout').open('wb') as stdout, (OUT / f'{label}.stderr').open('wb') as stderr:
        result = subprocess.run(list(map(str, argv)), cwd=cwd, stdout=stdout, stderr=stderr)
    save(OUT / f'{label}.exit.json', {'returncode': result.returncode})
    print(f'{label}: rc={result.returncode}', flush=True)
    return result.returncode


def entries(root):
    rows = []
    for raw in subprocess.check_output(['git', 'ls-files', '-s', '-z'], cwd=root).split(b'\0'):
        if not raw:
            continue
        info, path = raw.decode().split('\t', 1)
        mode, blob, stage = info.split()
        assert stage == '0', (path, stage)
        rows.append({'path': path, 'mode': mode, 'blob': blob})
    return rows


def snapshot(root, label):
    rows = entries(root)
    facts = {'head': git(root, 'rev-parse', 'HEAD'), 'index_tree': git(root, 'write-tree'), 'files': {}}
    with tarfile.open(OUT / f'{label}.tar.gz', 'w:gz') as archive:
        for row in rows:
            file = root / row['path']
            data = os.readlink(file).encode() if file.is_symlink() else file.read_bytes()
            facts['files'][row['path']] = {'sha256': digest(data), 'index_mode': row['mode'],
                                                'physical_mode': oct(file.lstat().st_mode & 0o7777)}
            archive.add(file, arcname=row['path'], recursive=False)
    save(OUT / f'{label}.json', facts)
    return facts


def prepare():
    save(OUT / 'results.json', [{'label': name, 'status': 'NOT_RUN'} for name in PLANNED])
    assert git(CORE, 'rev-parse', 'HEAD') == PINS['core_commit']
    assert git(CORE, 'rev-parse', 'HEAD^{tree}') == PINS['core_tree']
    assert git(CORE, 'rev-parse', PINS['target_commit'] + '^{tree}') == PINS['target_tree']
    rows = [r for r in entries(HUB) if r['path'] != '.github/workflows/catalog-check.yml'
            and not r['path'].startswith('qa/')]
    assert len(rows) == PINS['hub_product_paths']
    assert digest(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()) == PINS['hub_product_manifest_sha256']
    snapshot(CORE, 'core-before')
    snapshot(HUB, 'hub-before')
    # No merge commit, ref movement or conflict resolution. A conflict remains a preparation failure.
    rc = command('merge-tree', ['git', 'merge-tree', '--write-tree',
                                PINS['target_commit'], PINS['core_commit']], CORE)
    assert rc == 0, 'mechanical composition conflicted; read merge-tree streams'
    tree = (OUT / 'merge-tree.stdout').read_text().splitlines()[0].strip()
    assert len(tree) == 40 and all(c in '0123456789abcdef' for c in tree)
    assert command('clone-composed', ['git', 'clone', '--shared', '--no-checkout', str(CORE), str(COMPOSED)], TEMP) == 0
    assert command('read-composed-tree', ['git', 'read-tree', tree], COMPOSED) == 0
    assert command('materialize-composed', ['git', 'checkout-index', '-a'], COMPOSED) == 0
    assert git(COMPOSED, 'write-tree') == tree
    # One reviewed lock environment must really describe both subjects.
    for name in ('uv.lock', 'pyproject.toml', 'requirements-runtime.lock'):
        assert (CORE / name).read_bytes() == (COMPOSED / name).read_bytes(), f'{name} diverged: separate dependency setup required'
    snapshot(COMPOSED, 'composed-before')
    save(OUT / 'subjects.json', dict(PINS, hub_qa_commit=git(HUB, 'rev-parse', 'HEAD'),
                                    composed_tree=tree, composed_head_is_source_commit=True))


OWNERS = [
    'test_memory_reader', 'test_memory_reader_bounds', 'test_memory_reader_plugin',
    'test_chronicle_store', 'test_chronicle_import', 'test_chronicle_tools', 'test_chronicle_read_path',
    'test_memory_inventory', 'test_common_story', 'test_memory_floor_measurements',
    'test_memory_view_readers', 'test_memory_production_shape', 'test_memory_view_story',
    'test_memory_view_forms', 'test_child_chronicle_drafts', 'test_consciousness_memory_facts',
    'test_memory_fallback', 'test_memory_fallback_sources', 'test_memory_fallback_reachability',
    'test_memory_fallback_max', 'test_legacy_memory_notice', 'test_memory_story_labels',
]
NEGATIVE = {
    'c1': (['test_long_iso_period_is_bounded_in_every_api_reader_and_its_original_is_readable'],
           'assert all(len(json.dumps(e)) < PER_ENTRY'),
    'c3': (['test_historical_nonlegacy_transfer_is_replayed_not_revalidated'],
           'assert (refused.ok, refused.reason, refused.detail)'),
    'f3': ([f'test_f3_preserves_old_helper_row_incompleteness_and_prices_the_rendered_form[{case}]'
            for case in ('incomplete', 'mixed_last', 'mixed_first')],
           'assert expected in folded.split'),
}
RESULTS = (json.loads((OUT / 'results.json').read_text()) if (OUT / 'results.json').exists()
           else [{'label': name, 'status': 'NOT_RUN'} for name in PLANNED])


def safe_test_custody(label):
    """Read this launcher's announced terminal receipt, independently of test status."""
    custody = {'status': 'CUSTODY_ERROR', 'receipt': None, 'errors': []}
    try:
        lines = (OUT / f'{label}.stderr').read_text(errors='replace').splitlines()
        # Nested commands may announce their own runs. Only our explicit parent counts.
        parent = (OUT / label).resolve()
        paths = [Path(line.removeprefix('SAFE_TEST_EVIDENCE ')) for line in lines
                 if line.startswith('SAFE_TEST_EVIDENCE ')]
        paths = [p for p in paths if p.is_absolute() and p.resolve().parent == parent]
        if len(paths) != 1:
            raise ValueError(f'expected one SAFE_TEST_EVIDENCE under {parent}, found {len(paths)}')
        receipt = paths[0] / 'result.json'
        custody['receipt'] = str(receipt)
        raw = receipt.read_bytes()
        (OUT / f'{label}.safe-test-result.json').write_bytes(raw)
        facts = json.loads(raw)
        custody['facts'] = facts
        if (not isinstance(facts, dict) or not isinstance(facts.get('cleanup'), dict)
                or not isinstance(facts.get('retention_reason'), str)):
            raise ValueError('missing terminal cleanup/retention facts (pending is not terminal)')
        for field in ('containment_error', 'evidence_error'):
            if not isinstance(facts.get(field), str):
                custody['errors'].append(f'missing or invalid {field}')
            elif facts[field]:
                custody['errors'].append(f'{field}: {facts[field]}')
        custody['errors'].extend(line for line in lines
                                 if line.startswith('SAFE_TEST_EVIDENCE_INCOMPLETE '))
        # retention_reason/debug retention and kept roots are normal with --keep.
    except (OSError, ValueError) as exc:
        custody['errors'].append(f'{type(exc).__name__}: {exc}')
    if not custody['errors']:
        custody['status'] = 'PASS'
    save(OUT / f'{label}.custody.json', custody)
    return custody


def execute(label, core, cwd, argv, env=None, negative=None, collect=False):
    spec = OUT / f'{label}.input.json'
    save(spec, {'cwd': str(cwd), 'argv': argv, 'env': env or {}})
    timeout = 900 if collect else 5400 if label == 'atlas' else 1800
    wrapper = [sys.executable, '-I', '-S', str(core / 'scripts/safe_test.py'), '--keep',
               '--temp-parent', '/tmp', '--timeout', str(timeout), '--evidence-dir', str(OUT / label),
               '--', sys.executable, '-B', str(QA / 'child.py'), str(spec)]
    if not collect:
        RESULTS[PLANNED.index(label)] = {'label': label, 'status': 'STARTED'}
        save(OUT / 'results.json', RESULTS)
    rc = command(label, wrapper, core)
    custody = safe_test_custody(label)
    if collect:
        return rc, custody
    result = {'label': label, 'returncode': rc, 'status': 'EXECUTION_ERROR',
              'custody': custody, 'qualification_status': 'FAIL'}
    RESULTS[PLANNED.index(label)] = result
    save(OUT / 'results.json', RESULTS)
    if argv[:2] == ['-m', 'pytest']:
        junit = OUT / f'{label}.xml'
        if junit.is_file():
            suite = ET.parse(junit).getroot()
            cases = list(suite.iter('testcase'))
            failures = [c for c in cases if c.find('failure') is not None]
            errors = [c for c in cases if c.find('error') is not None]
            skips = [c for c in cases if c.find('skipped') is not None]
            result.update(cases=len(cases), failures=len(failures), errors=len(errors), skipped=len(skips))
            if negative:
                names, marker = negative
                good = (rc == 1 and len(cases) == len(names) and len(failures) == len(names)
                        and not errors and not skips and {c.get('name') for c in cases} == set(names)
                        and all(marker in ''.join(c.find('failure').itertext()) for c in failures))
                result['status'] = 'EXPECTED_FAILURE' if good else 'INVALID_NEGATIVE'
            elif rc == 0 and cases and not failures and not errors and not skips:
                result['status'] = 'PASS'
            elif failures and not errors and not skips and rc == 1:
                result['status'] = 'ASSERTION_FAILURE'
            else:
                result['status'] = 'INCOMPLETE_OR_SETUP_ERROR'
    elif rc == 0:
        result['status'] = 'PASS'
    expected_count = EXPECTED_COUNTS.get(label)
    if expected_count is not None and result.get('cases') != expected_count:
        result.update(status='INCOMPLETE_COLLECTION', expected_cases=expected_count)
    if result['status'] in ('PASS', 'EXPECTED_FAILURE') and custody['status'] == 'PASS':
        result['qualification_status'] = 'PASS'
    RESULTS[PLANNED.index(label)] = result
    save(OUT / 'results.json', RESULTS)
    assert result['qualification_status'] == 'PASS', result


def pytest(label, core, selection, cwd=None, env=None, negative=None):
    base = ['-m', 'pytest', '-o', 'addopts=', '-n', '0', '-p', 'no:cacheprovider', '-q', '-ra']
    rc, custody = execute(label + '-collect', core, cwd or core,
                          [*base, '--collect-only', *selection], env, collect=True)
    lines = (OUT / f'{label}-collect.stdout').read_text().splitlines()
    nodes = [line.strip() for line in lines if '::' in line and not line.startswith(' ')]
    expected = EXPECTED_COUNTS.get(label)
    collection_ok = rc == 0 and bool(nodes) and (expected is None or len(nodes) == expected)
    collection = {'returncode': rc, 'nodes': nodes, 'expected_count': expected,
                  'status': 'PASS' if collection_ok else 'COLLECTION_ERROR', 'custody': custody,
                  'qualification_status': 'PASS' if collection_ok and custody['status'] == 'PASS' else 'FAIL'}
    save(OUT / f'{label}-collection.json', collection)
    if collection['qualification_status'] != 'PASS':
        RESULTS[PLANNED.index(label)] = {'label': label, 'status': 'NOT_RUN' if collection_ok else 'COLLECTION_ERROR',
                                         'returncode': rc, 'collection': collection, 'qualification_status': 'FAIL'}
        save(OUT / 'results.json', RESULTS)
        raise RuntimeError(f'{label}: collection qualification failed; product cases NOT_RUN')
    execute(label, core, cwd or core, [*base, '--tb=long', '--durations=20',
            f'--junitxml={OUT / (label + ".xml")}', *selection], env, negative)


def run():
    assert (OUT / 'subjects.json').is_file(), 'preparation did not complete'
    assert sys.version_info[:3] == (3, 10, 22), sys.version
    import importlib.metadata
    import platform
    import sqlite3
    save(OUT / 'runtime.json', {'python': sys.version, 'executable': sys.executable,
         'platform': platform.platform(), 'architecture': platform.machine(), 'sqlite': sqlite3.sqlite_version,
         'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'base_executable': sys._base_executable,
         'git': subprocess.check_output(['git', '--version']).decode().strip(),
         'uv': subprocess.check_output(['uv', '--version']).decode().strip(),
         'node': subprocess.check_output(['node', '--version']).decode().strip(),
         'packages': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}})
    assert command('browser-inventory', [sys.executable, '-m', 'playwright', 'install', '--list'], CORE) == 0
    for label, core in [('frozen', CORE), ('composed', COMPOSED)]:
        pytest(f'{label}-transfer', core, ['tests/test_chronicle_transfer.py'])
        pytest(f'{label}-owners', core, [f'tests/{name}.py' for name in OWNERS])
    pytest('size', COMPOSED, ['-m', 'size_ratchet', 'tests/'],
           env={'OURO_SIZE_RATCHET_BASE_REF': PINS['target_commit']})
    # Extra witness is a named QA overlay, kept out of source tree snapshots and size measurement.
    compat = COMPOSED / 'tests/test_transfer_qa_compat.py'
    assert not compat.exists()
    shutil.copyfile(QA / 'test_transfer_compat.py', compat)
    save(OUT / 'compat-overlay.json', {'path': str(compat), 'sha256': digest(compat.read_bytes())})
    pytest('plugin-compat', COMPOSED, ['tests/test_transfer_qa_compat.py'])
    atlas_env = {'ATLAS_CORE': str(COMPOSED), 'ATLAS_RUN_ROOT': '/tmp/mt-atlas-suite',
                 'ATLAS_SHOTS': str(OUT / 'atlas-shots'), 'PYTHONPATH': str(HUB)}
    (OUT / 'atlas-shots').mkdir(exist_ok=True)
    pytest('atlas', COMPOSED, ['tests/memory_atlas'], cwd=HUB, env=atlas_env)
    execute('frames', COMPOSED, HUB, [str(QA / 'capture_frames.py'), str(HUB), str(COMPOSED),
                                    str(OUT / 'visual'), '/tmp/mt-atlas-frames'])
    # Each retained patch restores just one known defect; current tests/assertions remain byte-identical.
    for key, (nodes, marker) in NEGATIVE.items():
        mutant = TEMP / f'negative-{key}'
        shutil.copytree(CORE, mutant, ignore=shutil.ignore_patterns('.git', '__pycache__', '.venv', '_python_tmp'))
        patch = QA / 'negative' / f'{key}.patch'
        assert digest(patch.read_bytes()) == PINS['negative_patches'][key]
        assert command(f'{key}-patch-check', ['git', 'apply', '--check', str(patch)], mutant) == 0
        assert command(f'{key}-patch', ['git', 'apply', str(patch)], mutant) == 0
        tracked = [r['path'] for r in entries(CORE)]
        before = {name: digest((mutant / name).read_bytes()) for name in tracked if not (mutant / name).is_symlink()}
        changed = [name for name in before if before[name] != digest((CORE / name).read_bytes())]
        expected_file = {'c1': 'ouroboros/memory_reader.py', 'c3': 'ouroboros/chronicle_store.py',
                         'f3': 'ouroboros/memory_view_legacy.py'}[key]
        assert changed == [expected_file], changed
        with tarfile.open(OUT / f'negative-{key}-source.tar.gz', 'w:gz') as archive:
            for name in (expected_file, 'tests/test_chronicle_transfer.py'):
                archive.add(mutant / name, arcname=name)
        save(OUT / f'negative-{key}-before.json', before)
        try:
            pytest(f'negative-{key}', mutant, [f'tests/test_chronicle_transfer.py::{node}' for node in nodes], negative=(nodes, marker))
        finally:
            after = {name: digest((mutant / name).read_bytes()) if (mutant / name).is_file() else None for name in before}
            save(OUT / f'negative-{key}-after.json', after)
            with tarfile.open(OUT / f'negative-{key}-source-after.tar.gz', 'w:gz') as archive:
                for name in set(changed + ['tests/test_chronicle_transfer.py']) | {n for n in before if before[n] != after[n]}:
                    if (mutant / name).exists():
                        archive.add(mutant / name, arcname=name)
            assert before == after, 'negative source changed during the witness'
    assert all(r.get('qualification_status') == 'PASS' for r in RESULTS if r['label'] != 'datetime312'), 'qualification is red; read results.json'


def compare():
    assert sys.version_info[:3] == (3, 12, 13), sys.version
    save(OUT / 'runtime312.json', {'python': sys.version, 'executable': sys.executable,
                                 'prefix': sys.prefix, 'base_prefix': sys.base_prefix})
    pytest('datetime312', COMPOSED, [
        'tests/test_chronicle_transfer.py::test_long_iso_period_is_bounded_in_every_api_reader_and_its_original_is_readable',
        'tests/test_chronicle_transfer.py::test_derived_period_projection_names_a_real_source_not_a_stated_field',
        'tests/test_chronicle_transfer.py::test_transfer_period_is_stated_with_and_without_ready_units_across_readers',
    ])
    assert all(r.get('qualification_status') == 'PASS' for r in RESULTS)


def export():
    # Only exact roots named by our launcher; never search processes or remove data.
    retained = set()
    for stream in OUT.glob('*.stderr'):
        for line in stream.read_text(errors='replace').splitlines():
            if line.startswith('SAFE_TEST_RETAINED '):
                root = Path(line.removeprefix('SAFE_TEST_RETAINED ')).resolve()
                assert root.parent == Path('/tmp').resolve() and root.name.startswith('ob-'), root
                retained.add(root)
    for name in ('mt-atlas-suite', 'mt-atlas-frames'):
        root = Path('/tmp') / name
        if root.exists():
            retained.add(root)
    with tarfile.open(OUT / 'synthetic-state.tar.gz', 'w:gz') as archive:
        for root in sorted(retained):
            archive.add(root, arcname=root.name)
    audits = {}
    for root, label in ((CORE, 'core'), (HUB, 'hub'), (COMPOSED, 'composed')):
        before = OUT / f'{label}-before.json'
        if before.exists():
            after = snapshot(root, f'{label}-after')
            audits[label] = after == json.loads(before.read_text())
    save(OUT / 'source-audit.json', audits)
    save(OUT / 'preparation-outcomes.json', {k: os.environ.get(k, 'unknown') for k in
         ('PREPARE_OUTCOME', 'RUNTIME_OUTCOME', 'BROWSER_OUTCOME', 'COMPARISON_SETUP_OUTCOME')})
    save(OUT / 'SHA256SUMS.json', {str(p.relative_to(OUT)): file_digest(p)
         for p in sorted(OUT.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS.json'})
    assert all(audits.values()), 'source changed during qualification; inspect before/after archives'


if __name__ == '__main__':
    phase = sys.argv[1]
    try:
        {'prepare': prepare, 'run': run, 'compare': compare, 'export': export}[phase]()
    except BaseException:
        (OUT / f'{phase}-error.txt').write_text(traceback.format_exc())
        raise
