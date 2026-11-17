#!/usr/bin/env python3
"""Temporary manual QA for original SSH Workspace PR316.

Run only on GitHub-hosted Linux. Payload calls use synthetic local files,
explicit fake SSH executables, or mocked transport. No real SSH destination.
Execution copies, case directories and evidence stay beneath QA_DIR.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import traceback

HERE = Path(__file__).resolve().parent
HUB = HERE.parent.parent
OLD_COMMIT = 'cd918c577c440793aaa10883b47dda2819cc99c9'
CANDIDATE_COMMIT = '851db70d4cf03d31f65d1b6762c84c3bacb636ef'
OLD_REMOTE_SHA = '47011e175c8ebef0aeb3290240a05b53e082514671957bfced8ce2a3f05fdab1'
PINNED = {
    'witnesses.py': 'dbb73c64ecc6cd44e69c3dc440e89b0bbe68c0348c3c96969e2e9d18f36fa269',
    'fixed/remote.py': 'c1d67522b0664c4a9f6761b42bd1024374ca88e5b8e79fc9f240001325034eef',
    'fixed/tests.py': '61cfe478c5e7bc8a0e00b495e28cb9141a83138e239847c7ef887abc87a5f05d',
}
CANDIDATE_FILES = {
    'remote.py': '3b30990a09ccc46c297199087a2a9f00ddd0c6e09e1f5ea7ca3df869876f7a0c',
    'ssh_workspace.py': '17db2efe01b461f3ea825f647ef670150a1c81eba9357c746afa64893971e1dc',
    'tests.py': '3b91a81322fe9a687c3bcd760e3d290e456002f40265b8d08eaff935569ae21d',
}
BASE = None
EVIDENCE = None
WITNESS = None
CASE_NUMBER = 0
REPORT = {
    'schema_version': 1,
    'phase': 'preparation',
    'checks': [],
    'sha256': {},
    'staged_sources': {},
    'pinned_commits': {'old': OLD_COMMIT, 'candidate': CANDIDATE_COMMIT},
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save():
    target = EVIDENCE / 'report.json'
    temporary = EVIDENCE / 'report.json.tmp'
    temporary.write_text(
        json.dumps(REPORT, indent=2, sort_keys=True, ensure_ascii=True) + '\n',
        encoding='utf-8',
    )
    temporary.replace(target)


def note(name, ok, detail):
    REPORT['checks'].append({
        'name': name, 'status': 'pass' if ok else 'fail', 'evaluation': detail,
    })
    save()
    print('%s %s: %s' % ('PASS' if ok else 'FAIL', name, detail), flush=True)


def require(name, ok, detail):
    note(name, ok, detail)
    if not ok:
        raise RuntimeError(name + ': source qualification failed')


def ensure(condition, detail):
    if not condition:
        raise ValueError(detail)


def pin(name, path, expected=None):
    actual = digest(path)
    REPORT['sha256'][name] = actual
    require('sha256:' + name, expected is None or actual == expected,
            'actual=%s expected=%s' % (actual, expected))
    return actual


def run(name, command, expect, *, tree=None, op=None, strict=False, timeout=180):
    global CASE_NUMBER
    CASE_NUMBER += 1
    case = BASE / ('c%02d' % CASE_NUMBER)
    case.mkdir()
    temp = case / 't'
    temp.mkdir()
    work = case / 'w'
    work.mkdir()
    if op is not None:
        command = [sys.executable, '-B', WITNESS, op, tree, work]
    command = [str(item) for item in command]
    overrides = {
        'TMPDIR': str(temp), 'TMP': str(temp), 'TEMP': str(temp),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1',
        'GIT_OPTIONAL_LOCKS': '0',
    }
    removed = ['PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP',
               'PYTHONINSPECT', 'PYTHONOPTIMIZE', 'PYTHONIOENCODING']
    env = os.environ.copy()
    for key in removed:
        env.pop(key, None)
    if strict:
        overrides['PYTHONIOENCODING'] = 'utf-8:strict'
    env.update(overrides)
    stdout_path = EVIDENCE / ('%02d.stdout' % CASE_NUMBER)
    stderr_path = EVIDENCE / ('%02d.stderr' % CASE_NUMBER)
    record = {
        'name': name, 'command': command, 'cwd': str(case),
        'env_overrides': overrides, 'env_removed': removed,
        'stdout_file': stdout_path.name, 'stderr_file': stderr_path.name,
        'timeout_seconds': timeout, 'timed_out': False,
        'exit_code': None, 'status': 'running',
    }
    REPORT['checks'].append(record)
    save()
    try:
        # Regular files retain bytes as they arrive, including on timeout.
        with stdout_path.open('wb') as out, stderr_path.open('wb') as err:
            process = subprocess.Popen(
                command, cwd=case, env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err,
            )
            record['pid'] = process.pid
            save()
            try:
                record['exit_code'] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                record['timed_out'] = True
                # Only this exact child handle; no process search or group signal.
                process.kill()
                record['exit_code'] = process.wait(timeout=10)
    except Exception:
        record['error'] = traceback.format_exc()
    finally:
        for field, path in [('stdout', stdout_path), ('stderr', stderr_path)]:
            raw = path.read_bytes() if path.exists() else b''
            record[field] = raw.decode('utf-8', errors='backslashreplace')
            record[field + '_sha256'] = hashlib.sha256(raw).hexdigest()
        try:
            if record.get('error') or record['timed_out']:
                ok, detail = False, 'command error or timeout; raw output retained'
            else:
                ok, detail = expect(record)
        except Exception:
            ok, detail = False, 'validator exception: ' + traceback.format_exc()
        record.update(status='pass' if ok else 'fail', evaluation=detail)
        save()
        print('%s %s: %s' % ('PASS' if ok else 'FAIL', name, detail), flush=True)
    return ok


def expect_tests(record, negative=False):
    stderr = record['stderr']
    ran13 = re.search(r'^Ran 13 tests in [0-9.]+s$', stderr, re.M) is not None
    headers = [line for line in stderr.splitlines()
               if line.startswith(('FAIL: ', 'ERROR: '))]
    if negative:
        method = 'test_search_read_line_number_agreement'
        expected = [
            'FAIL: %s (__main__.WorkspaceTests.%s) (separator=%r, ending=%r)'
            % (method, method, '\r', ending)
            for ending in ('\n', '\r\n', '')
        ]
        ok = (record['exit_code'] == 1 and ran13 and headers == expected
              and stderr.count('AssertionError:') == 3
              and stderr.rstrip().endswith('FAILED (failures=3)'))
        return ok, 'expected exactly three bare-CR assertion failures; headers=%r' % headers
    ok = (record['exit_code'] == 0 and ran13 and not headers
          and stderr.rstrip().endswith('OK'))
    return ok, 'exit=%r ran13=%s' % (record['exit_code'], ran13)


def expect_help(record):
    ok = (record['exit_code'] == 0 and 'usage:' in record['stdout']
          and '--host' in record['stdout'])
    return ok, 'CLI help exit=%r' % record['exit_code']


def expect_witness(record, op):
    data = json.loads(record['stdout'])
    ok = record['exit_code'] == 0 and data.get('passed') is True
    if op == 'malformed':
        result = data.get('result', {})
        ok = (ok and data.get('invocations') == 'called\n'
              and result.get('outcome') == 'unknown'
              and result.get('error') == 'invalid_remote_json')
    else:
        ok = (ok and data.get('branch') == op.split('-')[1]
              and data.get('exception') is None)
    return ok, 'exit=%r passed=%r op=%s' % (record['exit_code'], data.get('passed'), op)


def expect_surrogate(record, strict):
    data = json.loads(record['stdout'])
    metadata = json.loads(record['stderr'])
    expected = {'ok': True, 'outcome': 'completed', 'result': {'name': 'bad\udcff'}}
    errors = 'strict' if strict else 'surrogateescape'
    ok = (record['exit_code'] == 0 and data == expected
          and record['stdout'].isascii() and '\\udcff' in record['stdout']
          and metadata.get('encoding', '').lower().replace('_', '-') == 'utf-8'
          and metadata.get('errors') == errors)
    return ok, 'exit=%r stdout metadata=%r expected_errors=%s' % (
        record['exit_code'], metadata, errors)


def expect_lines(record, version):
    rows = json.loads(record['stdout'])
    ensure(isinstance(rows, list) and len(rows) == 36, 'expected exactly 36 rows')
    failed = []
    index = 0
    for separator, value in [('lf', '\n'), ('cr', '\r'), ('ff', '\f'), ('u2028', '\u2028')]:
        layouts = ('same-line', 'later-line', 'minimal') if separator == 'cr' else ('same-line', 'later-line')
        for layout in layouts:
            if layout == 'minimal':
                content = b'alpha\rTARGET\n'
            elif layout == 'same-line':
                content = ('alpha' + value + 'TARGET\nTAIL\n').encode('utf-8')
            else:
                content = ('alpha' + value + 'beta\nTARGET\nTAIL\n').encode('utf-8')
            # All witness fixtures end in LF. Retain exact LF-terminated bytes.
            lf_chunks = [piece + b'\n' for piece in content.split(b'\n')[:-1]]
            lf_line = next(i for i, piece in enumerate(lf_chunks, 1) if b'TARGET' in piece)
            search_line = lf_line + int(version == 'old' and separator != 'lf')
            for limit in (1, 2):
                for start_kind in ('search-returned', 'independent-LF'):
                    row = rows[index]
                    index += 1
                    key = (separator, layout, limit, start_kind)
                    actual_key = tuple(row.get(k) for k in
                                       ('separator', 'layout', 'max_lines', 'start_kind'))
                    ensure(actual_key == key, 'unexpected or duplicate row: %r' % (actual_key,))
                    oracle = b''.join(lf_chunks[lf_line-1:lf_line-1+limit])
                    oracle_offset = sum(map(len, lf_chunks[:lf_line-1]))
                    start = search_line if start_kind == 'search-returned' else lf_line
                    offset = sum(map(len, lf_chunks[:start-1]))
                    selected = lf_chunks[start-1:]
                    # Explicit known byte-count defect; FF and U+2028 do not split bytes.
                    if version != 'fixed' and separator == 'cr' and start == 1:
                        selected = [b'alpha\r', b'TARGET\n']
                        if layout == 'same-line':
                            selected.append(b'TAIL\n')
                    observed = b''.join(selected[:limit])
                    passed = (search_line == lf_line and observed == oracle
                              and offset == oracle_offset)
                    ensure(row.get('passed') is passed, 'wrong verdict for %r' % (key,))
                    ensure(row.get('expected_line') == lf_line
                           and row.get('expected_text') == oracle.decode('utf-8')
                           and row.get('expected_offset') == oracle_offset,
                           'wrong LF oracle for %r' % (key,))
                    search = row['search']
                    read = row['read']
                    matches = search.get('result', {}).get('matches', [])
                    ensure(search.get('ok') is True and search.get('outcome') == 'completed'
                           and len(matches) == 1 and matches[0]['line'] == search_line,
                           'unexpected search result for %r' % (key,))
                    result = read.get('result', {})
                    ensure(read.get('ok') is True and read.get('outcome') == 'completed'
                           and result.get('text') == observed.decode('utf-8')
                           and result.get('start_byte') == offset
                           and result.get('next_byte') == offset + len(observed)
                           and result.get('bytes_returned') == len(observed),
                           'unexpected read result for %r' % (key,))
                    if not passed:
                        failed.append(key)
    expected_count = {'old': 28, 'candidate': 6, 'fixed': 0}[version]
    ok = (len(failed) == expected_count
          and record['exit_code'] == (1 if expected_count else 0))
    return ok, 'version=%s failed_rows=%r' % (version, failed)


def qualify_repo(label, root, commit, paths=()):
    def identity(record):
        values = record['stdout'].splitlines()
        ok = (record['exit_code'] == 0 and len(values) == 2
              and values[0] == commit and re.fullmatch(r'[0-9a-f]{40}', values[1]) is not None)
        return ok, 'expected commit=%s actual commit/tree=%r' % (commit, values)
    if not run('git-identity:' + label,
               ['git', '-C', root, 'rev-parse', 'HEAD', 'HEAD^{tree}'], identity):
        raise RuntimeError('Git identity failed: ' + label)
    command = ['git', '-C', root, 'status', '--porcelain', '--untracked-files=all']
    if paths:
        command += ['--', *paths]
    if not run('git-clean:' + label, command,
               lambda r: (r['exit_code'] == 0 and r['stdout'] == '', 'clean source checkout')):
        raise RuntimeError('Dirty source checkout: ' + label)


def stage(name, files):
    target = BASE / name
    target.mkdir()
    REPORT['staged_sources'][name] = {}
    for filename, source in files.items():
        destination = target / filename
        shutil.copyfile(source, destination)
        source_name = str(source.relative_to(HUB))
        actual = digest(destination)
        REPORT['staged_sources'][name][filename] = {
            'source': source_name, 'sha256': actual,
        }
        require('stage:%s/%s' % (name, filename), actual == digest(source), source_name)
    return target


def main():
    global BASE, EVIDENCE, WITNESS
    allowed = (os.environ.get('GITHUB_ACTIONS') == 'true'
               and os.environ.get('GITHUB_REPOSITORY') == 'razzant/OuroborosHub'
               and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
               and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
               and os.environ.get('RUNNER_OS') == 'Linux'
               and sys.platform == 'linux' and sys.version_info[:2] == (3, 11))
    if not allowed:
        raise SystemExit('Requires manual GitHub-hosted Linux QA with Python 3.11')
    BASE = Path(os.environ['QA_DIR']).resolve(strict=True)
    runner_temp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    if BASE.parent != runner_temp or not BASE.name.startswith('q.'):
        raise SystemExit('QA_DIR must be the fresh short directory beneath RUNNER_TEMP')
    EVIDENCE = BASE / 'evidence'
    EVIDENCE.mkdir(exist_ok=True)
    REPORT.update(
        context={key: os.environ.get(key) for key in
                 ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_REF', 'GITHUB_RUN_ID',
                  'GITHUB_RUN_ATTEMPT', 'RUNNER_OS', 'RUNNER_ENVIRONMENT')},
        python=sys.version, platform=platform.platform(),
        baseline_scope='Old remote.py LF control; candidate CLI is imported but not called',
    )
    save()
    try:
        old = HERE / 'payload-old'
        candidate = HERE / 'payload-candidate'
        hub_paths = ['.github/workflows/catalog-check.yml', 'qa/ssh-workspace-316/run.py']
        hub_paths += ['qa/ssh-workspace-316/' + name for name in PINNED]
        qualify_repo('qa', HUB, os.environ['GITHUB_SHA'], hub_paths)
        qualify_repo('old', old, OLD_COMMIT)
        qualify_repo('candidate', candidate, CANDIDATE_COMMIT)
        for name in hub_paths[:2]:
            pin('hub/' + name, HUB / name)
        for name, expected in PINNED.items():
            pin('hub/' + name, HERE / name, expected)
        for name, expected in CANDIDATE_FILES.items():
            pin('candidate/' + name, candidate / name, expected)
        pin('old/remote.py', old / 'remote.py', OLD_REMOTE_SHA)

        expected_fixed = (candidate / 'remote.py').read_bytes()
        replacements = [
            (b'import hashlib\n', b'import hashlib\nimport io\n'),
            (b'data.splitlines(keepends=True)', b'io.BytesIO(data).readlines()'),
            (b'raw.splitlines(keepends=True)', b'io.BytesIO(raw).readlines()'),
        ]
        for before, after in replacements:
            ensure(expected_fixed.count(before) == 1, 'ambiguous LF replacement')
            expected_fixed = expected_fixed.replace(before, after, 1)
        require('exact-three-line-LF-fix',
                expected_fixed == (HERE / 'fixed/remote.py').read_bytes(),
                'one import and two LF-only line-reader replacements')

        candidate_files = {name: candidate / name for name in CANDIDATE_FILES}
        trees = {
            'candidate': stage('candidate', candidate_files),
            'fixed': stage('fixed', {
                **candidate_files,
                'remote.py': HERE / 'fixed/remote.py',
                'tests.py': HERE / 'fixed/tests.py',
            }),
            'control': stage('control', {
                **candidate_files, 'tests.py': HERE / 'fixed/tests.py',
            }),
            'old': stage('old', {
                'remote.py': old / 'remote.py',
                'ssh_workspace.py': candidate / 'ssh_workspace.py',
            }),
        }
        WITNESS = stage('witness', {'witnesses.py': HERE / 'witnesses.py'}) / 'witnesses.py'
        # The unmodified author suite checks OpenSSH's short control-path limit.
        socket_example = BASE / 'c99/t/.sshws-test-xxxxxxxx/control'
        require('short-test-path', len(os.fsencode(socket_example)) <= 100,
                'maximum planned synthetic control path fits 100 bytes')
        REPORT['phase'] = 'checks'
        save()

        run('candidate-original-13-tests',
            [sys.executable, '-B', trees['candidate'] / 'tests.py'], expect_tests)
        run('candidate-cli-help',
            [sys.executable, '-B', trees['candidate'] / 'ssh_workspace.py', '--help'], expect_help)
        run('fixed-full-13-tests',
            [sys.executable, '-B', trees['fixed'] / 'tests.py'], expect_tests)
        run('candidate-expanded-regression-negative',
            [sys.executable, '-B', trees['control'] / 'tests.py'],
            lambda r: expect_tests(r, negative=True))
        for label in ('candidate', 'fixed'):
            for op in ('timeout-open', 'timeout-closed', 'malformed'):
                run(label + ':' + op, None,
                    lambda r, op=op: expect_witness(r, op), tree=trees[label], op=op)
            for strict in (False, True):
                run(label + ':surrogate-' + ('strict' if strict else 'normal'), None,
                    lambda r, strict=strict: expect_surrogate(r, strict),
                    tree=trees[label], op='surrogate', strict=strict)
        for label in ('old', 'candidate', 'fixed'):
            run(label + ':LF-lines', None,
                lambda r, label=label: expect_lines(r, label),
                tree=trees[label], op='lines')
        REPORT['phase'] = 'complete'
    except Exception:
        REPORT['phase'] = 'aborted'
        note('preparation-or-runner-error', False, traceback.format_exc())
    finally:
        if REPORT['phase'] not in ('complete', 'aborted'):
            REPORT['phase'] = 'interrupted'
        REPORT['failed_checks'] = [item['name'] for item in REPORT['checks']
                                   if item['status'] != 'pass']
        REPORT['passed'] = REPORT['phase'] == 'complete' and not REPORT['failed_checks']
        save()
    print('phase=%s failed=%r' % (REPORT['phase'], REPORT['failed_checks']), flush=True)
    return 0 if REPORT['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
