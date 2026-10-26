"""Token-vault consumer races. Synthetic credentials, no provider/network access."""
import json
import os
import sqlite3
import sys
import tempfile
import multiprocessing
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'skills/calendar'), str(ROOT/'skills/calendar/tests')]
import providers_google as g
from store import Store, DB_FILENAME
from model import now_utc, iso_utc

KEY = 'test-only-vault-key'
SECRETS = {'CALENDAR_TOKEN_KEY': KEY, 'GOOGLE_CALENDAR_CLIENT_ID': 'test-client'}

def token(access='old', refresh='r1'):
    return {'access_token': access, 'refresh_token': refresh, 'expires_at': iso_utc(now_utc()+timedelta(hours=1))}

def connect_process(path, email, ready, go):
    store = Store(path)
    state = g.start_auth(store, SECRETS, 'http://127.0.0.1/callback')['state']
    def primary(adapter):
        ready.put(email)
        if not go.wait(10): raise RuntimeError('parent did not release HTTP barrier')
        return email
    with patch.object(g, '_token_request', return_value={**token(email, email), 'expires_in': 3600}), patch.object(g.GoogleAdapter, 'primary_email', primary):
        g.finish_auth(store, SECRETS, path, 'code', state)

class VaultR5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name
        self.store = Store(self.path)
        g.save_tokens(self.path, KEY, {'a@example.test': token()})
        self.adapter = g.GoogleAdapter.for_account(SECRETS, self.path, 'a@example.test')

    def finish(self, email, callback=None, refresh='new-refresh'):
        started = g.start_auth(self.store, SECRETS, 'http://127.0.0.1/callback')
        def primary(adapter):
            if callback: callback()
            return email
        with patch.object(g, '_token_request', return_value={**token('new-access', refresh), 'expires_in': 3600}), \
             patch.object(g.GoogleAdapter, 'primary_email', primary):
            return g.finish_auth(self.store, SECRETS, self.path, 'test-code', started['state'])

    def test_oauth_completion_keeps_refresh_committed_during_email_http(self):
        fired = []
        def refresh():
            fired.append(True)
            self.adapter._persist({'access_token': 'fresh-a'})
        self.finish('b@example.test', refresh)
        self.assertEqual(fired, [True])
        tokens = g.load_tokens(self.path, KEY)
        self.assertEqual(set(tokens), {'a@example.test', 'b@example.test'})
        self.assertEqual(tokens['a@example.test']['access_token'], 'fresh-a')

    def test_refresh_after_disconnect_does_not_resurrect_account(self):
        self.assertTrue(g.forget_account(self.path, KEY, 'a@example.test'))
        self.adapter._persist({'access_token': 'obsolete-refresh-result'})
        self.assertNotIn('a@example.test', g.load_tokens(self.path, KEY))

    def test_old_adapter_cannot_overwrite_reconnected_account(self):
        self.finish('a@example.test', refresh='replacement-r')
        self.adapter._persist({'access_token': 'obsolete-refresh-result'})
        tokens = g.load_tokens(self.path, KEY)
        self.assertEqual(tokens['a@example.test']['refresh_token'], 'replacement-r')
        self.assertEqual(tokens['a@example.test']['access_token'], 'new-access')

    def test_reconnect_with_same_refresh_token_still_retires_old_adapter(self):
        self.finish('a@example.test', refresh='r1')
        self.adapter._persist({'access_token': 'obsolete'})
        self.assertEqual(g.load_tokens(self.path, KEY)['a@example.test']['access_token'], 'new-access')

    def test_two_process_oauth_completions_merge_both_accounts(self):
        mp = multiprocessing.get_context('spawn')
        ready, go = mp.Queue(), mp.Event()
        processes = [mp.Process(target=connect_process, args=(self.path, name, ready, go)) for name in ('b@example.test','c@example.test')]
        try:
            for process in processes: process.start()
            self.assertEqual({ready.get(timeout=10), ready.get(timeout=10)}, {'b@example.test','c@example.test'})
            go.set()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(set(g.load_tokens(self.path, KEY)), {'a@example.test','b@example.test','c@example.test'})
        finally:
            go.set()
            for process in processes:
                if process.is_alive(): process.terminate()
                process.join(5)
            ready.close()
            ready.join_thread()

    def test_each_vault_commit_uses_its_own_private_tempfile(self):
        names = []
        replace = g.os.replace
        def record(source, target):
            names.append(source)
            self.assertEqual(os.stat(source).st_mode & 0o777, 0o600)
            return replace(source, target)
        with patch.object(g.os, 'replace', record):
            self.adapter._persist({'access_token': 'a1'})
            self.adapter._persist({'access_token': 'a2'})
        self.assertEqual(len(set(names)), 2)

    def test_network_callbacks_have_no_sqlite_writer_lock(self):
        observed = []
        def network(body):
            with sqlite3.connect(os.path.join(self.path, DB_FILENAME), timeout=0) as conn:
                conn.execute('BEGIN IMMEDIATE')
                observed.append(True)
            return {**token('new', 'new-r'), 'expires_in': 3600}
        state = g.start_auth(self.store, SECRETS, 'http://127.0.0.1/callback')['state']
        with patch.object(g, '_token_request', network), patch.object(g.GoogleAdapter, 'primary_email', return_value='b@example.test'):
            g.finish_auth(self.store, SECRETS, self.path, 'code', state)
        self.assertEqual(observed, [True])

    def test_failed_replace_preserves_original_ciphertext_and_removes_temporary(self):
        path = Path(self.path, g.TOKENS_FILE); before = path.read_bytes()
        with patch.object(g.os, 'replace', side_effect=OSError('deliberate write failure')):
            with self.assertRaises(OSError):
                self.adapter._persist({'access_token': 'not-published'})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(Path(self.path).glob('*.tmp')), [])

if __name__ == '__main__': unittest.main()
