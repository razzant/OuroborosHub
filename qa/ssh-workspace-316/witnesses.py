"""Small source-bound witnesses, executed only by the approved gated runner."""
import ast
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from unittest.mock import patch

SOURCE = Path(sys.argv[2]).resolve()
WORK = Path(sys.argv[3]).resolve()
sys.path.insert(0, str(SOURCE))
import remote
import ssh_workspace as cli


def lines():
    rows = []
    for name, separator in [('lf', '\n'), ('cr', '\r'), ('ff', '\f'), ('u2028', '\u2028')]:
        for layout in (('same-line', 'later-line', 'minimal') if name == 'cr' else ('same-line', 'later-line')):
            content = ('alpha\rTARGET\n' if layout == 'minimal' else
                       'alpha' + separator + 'TARGET\nTAIL\n' if layout == 'same-line' else
                       'alpha' + separator + 'beta\nTARGET\nTAIL\n')
            data = content.encode('utf-8')
            p = WORK / (name + '-' + layout + '.txt')
            p.write_bytes(data)
            envelope = lambda req: dict(root=str(WORK), request=dict(path=p.name, **req))
            search = remote.main(envelope(dict(op='search', query='TARGET')))
            # Independent LF-only oracle, including exact returned bytes and offsets.
            pieces = data.split(b'\n')
            expected_line = next(i + 1 for i, piece in enumerate(pieces) if b'TARGET' in piece)
            matches = search.get('result', {}).get('matches', [])
            for limit in (1, 2):
                for start_kind in ('search-returned', 'independent-LF'):
                    start = (matches[0]['line'] if matches else None) if start_kind == 'search-returned' else expected_line
                    read = remote.main(envelope(dict(op='read', start_line=start, max_lines=limit)))
                    offset = sum(len(piece) + 1 for piece in pieces[:expected_line-1])
                    selected = pieces[expected_line-1:expected_line-1+limit]
                    expected = b'\n'.join(selected)
                    if expected_line-1+len(selected) < len(pieces):
                        expected += b'\n'
                    result = read.get('result', {})
                    passed = (len(matches) == 1 and matches[0]['line'] == expected_line
                              and result.get('text') == expected.decode('utf-8')
                              and result.get('start_byte') == offset
                              and result.get('next_byte') == offset + len(expected))
                    rows.append(dict(separator=name, layout=layout, max_lines=limit,
                                     start_kind=start_kind, search=search, read=read,
                                     expected_line=expected_line, expected_text=expected.decode(),
                                     expected_offset=offset, passed=passed))
    print(json.dumps(rows, ensure_ascii=True, indent=2))
    return 0 if all(row['passed'] for row in rows) else 1


def timeout(branch):
    retained, denied_calls, result = [], [], None
    real_popen = subprocess.Popen
    source = (SOURCE / 'remote.py').read_text()
    kill_lines = sorted(n.lineno for n in ast.walk(ast.parse(source))
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'killpg')
    expected_callsite = kill_lines[0 if branch == 'open' else 1]
    def retain(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        retained.append(child)
        return child
    def denied(pid, sig):
        assert len(retained) == 1 and pid == retained[0].pid
        frame = inspect.currentframe().f_back
        while frame and frame.f_code.co_filename != remote.__file__:
            frame = frame.f_back
        denied_calls.append(dict(pid=pid, sig=int(sig), callsite=frame.f_lineno if frame else None))
        raise PermissionError('QA injected denial; no signal sent')
    program = ('import os,time; os.write(1,b"OUT-before\\n"); os.write(2,b"ERR-before\\n"); '
               + ('os.close(1); os.close(2); ' if branch == 'closed' else '')
               + 'time.sleep(2); os._exit(23)')
    exception = None
    try:
        with patch.object(remote.subprocess, 'Popen', side_effect=retain), \
             patch.object(remote.os, 'killpg', side_effect=denied):
            result = remote.execute(WORK, dict(argv=[sys.executable, '-B', '-c', program],
                                              timeout=1, max_bytes=100))
    except Exception as error:
        exception = dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
    # Wait on the exact retained handle; never infer group ownership or resignal it.
    final_rcs = [child.wait(timeout=6) for child in retained]
    expected_streams = {'stdout': 'OUT-before\n', 'stderr': 'ERR-before\n'}
    passed = bool(result and result.get('returncode') == 23 and result.get('timed_out')
                  and result.get('group_stop') == 'failed'
                  and 'PermissionError' in (result.get('group_stop_error') or '')
                  and result.get('process_exit_verified') is True
                  and result.get('descendants_exit_verified') is False
                  and final_rcs == [23] and len(denied_calls) == 1
                  and denied_calls[0]['callsite'] == expected_callsite)
    for name, expected in expected_streams.items():
        stream = (result or {}).get('streams', {}).get(name, {})
        passed = passed and (stream.get('text') == expected and
                             stream.get('bytes_observed') == len(expected.encode()) and
                             stream.get('shortened') is False)
    print(json.dumps(dict(branch=branch, expected_callsite=expected_callsite,
                          denied_calls=denied_calls, result=result, exception=exception,
                          child_returncodes=final_rcs, passed=bool(passed)), indent=2))
    return 0 if passed else 1


def surrogate():
    request = WORK / 'request.json'
    request.write_text('{}')
    print(json.dumps(dict(encoding=sys.stdout.encoding, errors=sys.stdout.errors)), file=sys.stderr)
    with patch.object(cli, 'call', return_value=dict(ok=True, outcome='completed',
                                                   result=dict(name='bad\udcff'))), \
         patch.object(sys, 'argv', ['ssh_workspace.py', '--host', 'fake', '--root', str(WORK),
                                   '--request', str(request)]):
        return cli.main()


def malformed():
    # Author tests mock transport; they do not count invocations after bad JSON.
    fake = WORK / 'fake-ssh'
    count = WORK / 'invocations'
    fake.write_text('#!' + sys.executable + '\n'
                    'import ast,re,sys\nfrom pathlib import Path\n'
                    'p=Path(' + repr(str(count)) + ')\n'
                    'with p.open("ab") as f: f.write(b"called\\n")\n'
                    'data=sys.stdin.buffer.read()\n'
                    'marker=re.search(rb"SSHWS_[0-9a-f]+:",data).group().decode()\n'
                    'print(marker+"{invalid json")\n')
    fake.chmod(0o700)
    result = cli.call('fake', str(WORK), dict(op='list'), ssh=str(fake))
    invocations = count.read_bytes()
    passed = (result.get('outcome') == 'unknown' and result.get('error') == 'invalid_remote_json'
              and invocations == b'called\n')
    print(json.dumps(dict(result=result, invocations=invocations.decode(), passed=passed), indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    op = sys.argv[1]
    if op == 'lines':
        sys.exit(lines())
    if op in ('timeout-open', 'timeout-closed'):
        sys.exit(timeout(op.split('-')[1]))
    if op == 'surrogate':
        sys.exit(surrogate())
    if op == 'malformed':
        sys.exit(malformed())
    raise SystemExit('unknown witness')
