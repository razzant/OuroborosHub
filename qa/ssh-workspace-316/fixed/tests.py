"""Offline contract tests; all mutations use temporary local directories."""
import hashlib
import io
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.dont_write_bytecode = True
import remote
import ssh_workspace as cli


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.sshws-test-')
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def runop(self, **req):
        return remote.main(dict(root=str(self.root), request=req))
    def test_cycle(self):
        r = self.runop(op='create', path='a b.txt', content='Привет\nvalue=1\n')
        self.assertTrue(r['ok'])
        digest = r['result']['sha256']
        self.assertFalse(self.runop(op='create', path='a b.txt', content='clobber')['ok'])
        read = self.runop(op='read', path='a b.txt', start_line=2)['result']
        self.assertEqual(read['text'], 'value=1\n')
        self.assertEqual(read['sha256'], digest)
        self.assertEqual(len(self.runop(op='list')['result']['entries']), 1)
        self.assertEqual(len(self.runop(op='search', query='value')['result']['matches']), 1)
        self.assertFalse(self.runop(op='patch', path='a b.txt', expected_sha256=digest,
                                   edits=[dict(old='value', new='x', count=2)])['ok'])
        edit = self.runop(op='patch', path='a b.txt', expected_sha256=digest,
                         edits=[dict(old='value=1', new='value=2', count=1)])['result']
        self.assertEqual(Path(edit['backup']).read_text(), 'Привет\nvalue=1\n')
        self.assertFalse(self.runop(op='patch', path='a b.txt', expected_sha256=digest,
                                   edits=[dict(old='value=2', new='oops')])['ok'])
        self.assertEqual((self.root/'a b.txt').read_text(), 'Привет\nvalue=2\n')
        self.assertEqual(self.runop(op='log-tail', path='a b.txt', max_lines=1)['result']['text'], 'value=2\n')
    def test_paths_special(self):
        self.assertFalse(self.runop(op='create', path='../outside', content='no')['ok'])
        (self.root/'link').symlink_to('/etc')
        self.assertFalse(self.runop(op='read', path='link/passwd')['ok'])
        os.mkfifo(self.root/'pipe')
        self.assertFalse(self.runop(op='read', path='pipe')['ok'])
        (self.root/'plain').write_text('x')
        (self.root/'alias').symlink_to(self.root/'plain')
        digest = hashlib.sha256(b'x').hexdigest()
        self.assertFalse(self.runop(op='patch', path='alias', expected_sha256=digest,
                                   edits=[dict(old='x', new='y')])['ok'])
        os.link(self.root/'plain', self.root/'hard')
        self.assertFalse(self.runop(op='patch', path='hard', expected_sha256=digest,
                                   edits=[dict(old='x', new='y')])['ok'])
    def test_invalid_create(self):
        r = self.runop(op='create', path='no-file', content=None)
        self.assertEqual(r['outcome'], 'error')
        self.assertFalse((self.root/'no-file').exists())

    def test_bounds(self):
        (self.root/'long').write_text('x'*100000)
        r = self.runop(op='read', path='long', max_bytes=17)['result']
        self.assertEqual(len(r['text']), 17)
        self.assertTrue(r['more'])
        self.assertEqual(r['next_byte'], 17)
        r = self.runop(op='log-tail', path='long', max_bytes=17)['result']
        self.assertEqual(len(r['text']), 17)
        self.assertTrue(r['partial_first_line'])
        self.assertTrue(r['omitted_prefix'])
        (self.root/'matches').write_text('abc\n'*20)
        r = self.runop(op='search', query='abc', limit=2)['result']
        self.assertEqual(len(r['matches']), 2)
        self.assertFalse(r['complete'])
        self.assertFalse(self.runop(op='read', path='long', max_bytes=True)['ok'])
    def test_search_read_line_number_agreement(self):
        p = self.root/'mixed-lines.txt'
        for separator in ('', '\r', '\x0c', '\u2028'):
            for ending in ('\n', '\r\n', ''):
                with self.subTest(separator=separator, ending=ending):
                    prefix = '\u03c0\x0c\u2028\n'.encode('utf-8')
                    expected = ('alpha' + separator + 'TARGET' + ending).encode('utf-8')
                    p.write_bytes(prefix + expected)
                    result = self.runop(op='search', path=p.name,
                                        query='TARGET')['result']
                    self.assertEqual([m['line'] for m in result['matches']], [2])
                    read = self.runop(op='read', path=p.name,
                                      start_line=result['matches'][0]['line'], max_lines=1)['result']
                    self.assertEqual(read['text'], expected.decode('utf-8'))
                    start = len(prefix)
                    self.assertEqual(read['start_byte'], start)
                    self.assertEqual(read['next_byte'], start + len(expected))
                    self.assertEqual(read['bytes_returned'], len(expected))
                    tail = self.runop(op='log-tail', path=p.name,
                                      max_lines=1)['result']
                    self.assertEqual(tail['text'], expected.decode('utf-8'))
                    self.assertEqual(tail['start_byte'], start)
                    self.assertEqual(tail['next_byte'], start + len(expected))
                    cap = 6
                    bounded = self.runop(op='read', path=p.name,
                                         start_line=2, max_lines=1, max_bytes=cap)['result']
                    self.assertEqual(bounded['text'],
                                     expected[:cap].decode('utf-8', errors='replace'))
                    self.assertEqual(bounded['bytes_returned'], cap)
                    self.assertEqual(bounded['next_byte'], start + cap)
                    tail_cap = len(expected) - 1
                    tail = self.runop(op='log-tail', path=p.name,
                                      max_lines=1, max_bytes=tail_cap)['result']
                    self.assertEqual(tail['text'], expected[1:].decode('utf-8'))
                    self.assertEqual(tail['bytes_returned'], tail_cap)
                    self.assertEqual(tail['start_byte'], start + 1)
                    self.assertEqual(tail['next_byte'], start + len(expected))

    def test_exec(self):
        r = self.runop(op='exec', argv=[sys.executable, '-c',
            'import sys; print(sys.argv[1]); sys.stderr.write("e"*10000); sys.exit(7)',
            '$(touch NEVER)'], timeout=3, max_bytes=100)['result']
        self.assertEqual(r['returncode'], 7)
        self.assertIn('$(touch NEVER)', r['streams']['stdout']['text'])
        self.assertTrue(r['streams']['stderr']['shortened'])
        self.assertFalse((self.root/'NEVER').exists())
        r = self.runop(op='exec', argv=[sys.executable, '-c', 'import time; time.sleep(10)'], timeout=1)['result']
        self.assertTrue(r['timed_out'])
        self.assertEqual(r['returncode'], -9)
        self.assertTrue(r['process_exit_verified'])
        self.assertEqual(r['group_stop'], 'signal_sent')
    def test_fake_ssh_end_to_end(self):
        fake = self.root/'fake-ssh'
        fake.write_text('#!'+sys.executable+'\nimport subprocess,sys\n'
                        'sys.exit(subprocess.call(sys.argv[-1],shell=True))\n')
        fake.chmod(448)
        r = cli.call('testbox', str(self.root), dict(op='create', path='quoted\".txt', content='hi\n世界'), ssh=str(fake))
        self.assertTrue(r['ok'], r)
        r = cli.call('testbox', str(self.root), dict(op='read', path='quoted\".txt'), ssh=str(fake))
        self.assertEqual(r['result']['text'], 'hi\n世界')
    def test_incremental_log(self):
        p = self.root/'growth.log'
        p.write_bytes('a\n世界\n'.encode())
        chunks, cursor = [], None
        while True:
            req = dict(op='log-read', path=p.name, max_bytes=1)
            if cursor is not None:
                req['cursor'] = cursor
            r = self.runop(**req)['result']
            self.assertFalse(r['reset_required'])
            chunks.append(base64.b64decode(r['data_base64']))
            cursor = r['cursor']
            if not r['more']:
                break
        self.assertEqual(b''.join(chunks), p.read_bytes())
        r = self.runop(op='log-read', path=p.name, cursor=cursor)['result']
        self.assertEqual(r['bytes_read'], 0)
        with p.open('ab') as f:
            f.write(b'next\n')
        r = self.runop(op='log-read', path=p.name, cursor=cursor)['result']
        self.assertEqual(r['text'], 'next\n')
        cursor = r['cursor']
        p.write_bytes(b'x')
        self.assertEqual(self.runop(op='log-read', path=p.name, cursor=cursor)['result']['reason'], 'truncated')
        p.write_bytes(b'x'*cursor['offset'])
        self.assertEqual(self.runop(op='log-read', path=p.name, cursor=cursor)['result']['reason'], 'anchor_changed')
        p.rename(self.root/'old.log')
        p.write_bytes(b'new\n')
        self.assertEqual(self.runop(op='log-read', path=p.name, cursor=cursor)['result']['reason'], 'identity_changed')
        r = self.runop(op='log-read', path=p.name, start='end')['result']
        self.assertEqual(r['bytes_read'], 0)
        self.assertEqual(r['next_byte'], 4)
    def test_log_large_and_invalid(self):
        p = self.root/'sparse.log'
        with p.open('wb') as f:
            f.truncate(128*1024*1024)
        r = self.runop(op='log-read', path=p.name, start='end')['result']
        self.assertEqual(r['next_byte'], 128*1024*1024)
        with p.open('ab') as f:
            f.write(b'large-tail\n')
        r = self.runop(op='log-read', path=p.name, cursor=r['cursor'])['result']
        self.assertEqual(r['text'], 'large-tail\n')
        for fields in [dict(cursor=[]), dict(cursor={}), dict(max_bytes=True),
                       dict(cursor=r['cursor'], start='end')]:
            self.assertFalse(self.runop(op='log-read', path=p.name, **fields)['ok'])
        os.mkfifo(self.root/'fifo')
        self.assertFalse(self.runop(op='log-read', path='fifo')['ok'])
    def test_mux_commands(self):
        self.root.chmod(0o700)
        socket = str(self.root/'control')
        master = cli.ssh_command('testbox', control_path=socket, master=True)
        self.assertIn('-N', master)
        self.assertNotIn('-M', master)  # duplicate master selectors enable confirmation/ask
        self.assertIn('ControlPersist=no', master)
        self.assertIn('ForkAfterAuthentication=no', master)
        slave = cli.ssh_command('testbox', control_path=socket)
        self.assertIn('ProxyCommand=false', slave)
        self.assertIn('ControlMaster=no', slave)
        self.assertNotIn('-M', slave)
        self.assertIn('ControlPath=none', cli.ssh_command('testbox'))
        Path(socket).write_text('stale')
        with self.assertRaises(ValueError):
            cli.ssh_command('testbox', control_path=socket, master=True)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            cli.ssh_command('testbox', control_path=socket)
    def test_exec_timeout_kill_denied_preserves_observations(self):
        def denied(pid, sig):
            raise PermissionError('not permitted')
        with patch.object(remote.os, 'killpg', side_effect=denied):
            r = remote.execute(self.root, dict(argv=[sys.executable, '-c',
                'import time; print("started", flush=True); time.sleep(3)'], timeout=1, max_bytes=100))
        self.assertTrue(r['timed_out'])
        self.assertEqual(r['group_stop'], 'failed')
        self.assertIn('PermissionError', r['group_stop_error'])
        self.assertIn('started', r['streams']['stdout']['text'])
        self.assertTrue(r['process_exit_verified'])

    def test_cli_json_escapes_surrogate_filenames(self):
        output = io.BytesIO()
        request_path = self.root/'request.json'
        request_path.write_text('{}')
        text_stdout = io.TextIOWrapper(output, encoding='utf-8', errors='strict')
        with patch.object(cli, 'call', return_value=dict(ok=True, outcome='completed',
                result=dict(entries=[dict(name='bad\udcff', kind='file')]))), \
             patch.object(cli.sys, 'argv', ['ssh_workspace.py', '--host', 'testbox', '--root', str(self.root), '--request', str(self.root/'request.json')]), \
             patch.object(cli.sys, 'stdout', text_stdout):
            self.assertEqual(cli.main(), 0)
            text_stdout.flush()
        encoded = output.getvalue()
        decoded = encoded.decode('utf-8', errors='strict')
        self.assertIn('\\udcff', decoded)
        self.assertEqual(json.loads(decoded)['result']['entries'][0]['name'], 'bad\udcff')

    def test_transport_unknown(self):
        with patch.object(cli, 'transport', return_value=(subprocess.CompletedProcess([],255,b'',b'connection lost'), {'stdout':0,'stderr':15})):
            self.assertEqual(cli.call('testbox', str(self.root), {'op':'list'})['outcome'], 'unknown')
        with patch.object(cli, 'transport', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            self.assertEqual(cli.call('testbox', str(self.root), {'op':'list'})['outcome'], 'unknown')
        with self.assertRaises(ValueError):
            cli.call('-oProxyCommand=bad', str(self.root), {'op':'list'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
