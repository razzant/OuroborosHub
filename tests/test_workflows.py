"""Publication sequencing, API hygiene and workflow privilege separation."""
from __future__ import annotations

import base64
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

import yaml

from scripts import ci_checks, sync_mirror
from scripts.github_api import APIError, GitHub, SafeRedirect

ROOT = Path(__file__).resolve().parents[1]


class MirrorTests(unittest.TestCase):
    def test_generated_main_is_published_before_the_same_commit_is_mirrored(self):
        with tempfile.TemporaryDirectory(prefix='hub-mirror-') as tmp:
            root = Path(tmp)
            (root / 'catalog.json').write_text('old', encoding='utf-8')
            calls = []
            def git(_root, args, token=None):
                calls.append((args, token))
                return 'main' if args == ['branch', '--show-current'] else ''
            with patch.object(sync_mirror, 'git', side_effect=git), patch.object(sync_mirror, 'build_catalog', return_value={'skills': []}):
                sync_mirror.sync(root, 'repo-synthetic', 'mirror-synthetic')
                self.assertEqual([args[0] for args, _token in calls], ['branch', 'add', '-c', 'push', 'push'])
                self.assertEqual(calls[-2], (['push', 'https://github.com/razzant/OuroborosHub.git', 'HEAD:refs/heads/main'], 'repo-synthetic'))
                self.assertEqual(calls[-1], (['push', 'https://github.com/joi-lab/OuroborosHub.git', 'HEAD:refs/heads/main'], 'mirror-synthetic'))
                self.assertIn('user.name=Ouroboros', calls[2][0])
                calls.clear()
                sync_mirror.sync(root, 'repo-synthetic', 'mirror-synthetic')
                self.assertEqual([args[0] for args, _token in calls], ['branch', 'push'])
                self.assertEqual(calls[-1][1], 'mirror-synthetic')

    def test_wrong_branch_or_invalid_catalog_never_reaches_a_push(self):
        with tempfile.TemporaryDirectory(prefix='hub-mirror-') as tmp:
            root = Path(tmp)
            for branch, failure in [('feature', None), ('main', 'validation')]:
                (root / 'catalog.json').write_text('old', encoding='utf-8')
                calls = []
                def git(_root, args, token=None):
                    calls.append(args)
                    if args[0] == 'push':
                        raise ValueError('synthetic push failure')
                    return branch
                with patch.object(sync_mirror, 'git', side_effect=git), patch.object(sync_mirror, 'build_catalog') as build:
                    build.return_value = {'skills': []}
                    if failure == 'validation':
                        build.side_effect = ValueError('invalid')
                    with self.assertRaises(ValueError):
                        sync_mirror.sync(root, 'repo-synthetic', 'mirror-synthetic')
                self.assertFalse(any('https://github.com/joi-lab/OuroborosHub.git' in args for args in calls))
            with self.assertRaises(ValueError):
                sync_mirror.sync(root, '', 'mirror-synthetic')

    def test_ssh_deploy_key_or_https_token_publish_then_mirror_with_separate_credentials(self):
        for deploy_key in ('synthetic-private-key', None):
            with self.subTest(deploy_key=bool(deploy_key)), tempfile.TemporaryDirectory(prefix='hub-ssh test-') as tmp:
                root, calls, key_paths = Path(tmp), [], []
                (root / 'catalog.json').write_text('old', encoding='utf-8')
                def transport(args, directory, *, env):
                    self.assertEqual(directory, root)
                    calls.append((args, env))
                    if args[0] == 'push' and args[1].startswith('git@'):
                        command = shlex.split(env['GIT_SSH_COMMAND'])
                        key = Path(command[command.index('-i') + 1])
                        hosts = Path(next(part.split('=', 1)[1] for part in command if part.startswith('UserKnownHostsFile=')))
                        self.assertTrue(key.is_relative_to(root))
                        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
                        self.assertEqual(key.read_text(encoding='utf-8'), deploy_key + '\n')
                        self.assertEqual(hosts.read_text(encoding='utf-8'), sync_mirror.GITHUB_HOST_KEY)
                        self.assertTrue({'BatchMode=yes', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes',
                                         'GlobalKnownHostsFile=/dev/null'} <= set(command))
                        self.assertNotIn('GIT_CONFIG_VALUE_0', env)
                        self.assertNotIn(deploy_key, str(args) + env['GIT_SSH_COMMAND'])
                        key_paths.extend([key, hosts])
                    return 'main' if args == ['branch', '--show-current'] else ''
                with patch.dict(os.environ, {'RUNNER_TEMP': tmp}), \
                        patch.object(sync_mirror, 'run_git', side_effect=transport), \
                        patch.object(sync_mirror, 'build_catalog', return_value={'skills': []}):
                    # A deploy key works even without GITHUB_TOKEN.
                    sync_mirror.sync(root, None if deploy_key else 'repo-synthetic', 'mirror-synthetic', deploy_key)
                pushes = [(args, env) for args, env in calls if args[0] == 'push']
                self.assertEqual(len(pushes), 2)
                expected_url = 'git@github.com:razzant/OuroborosHub.git' if deploy_key else 'https://github.com/razzant/OuroborosHub.git'
                self.assertEqual(pushes[0][0], ['push', expected_url, 'HEAD:refs/heads/main'])
                if not deploy_key:
                    self.assertNotIn('GIT_SSH_COMMAND', pushes[0][1])
                    self.assertEqual(base64.b64decode(pushes[0][1]['GIT_CONFIG_VALUE_0'].split()[-1]),
                                     b'x-access-token:repo-synthetic')
                self.assertEqual(pushes[1][0], ['push', 'https://github.com/joi-lab/OuroborosHub.git', 'HEAD:refs/heads/main'])
                self.assertNotIn('GIT_SSH_COMMAND', pushes[1][1])
                self.assertEqual(base64.b64decode(pushes[1][1]['GIT_CONFIG_VALUE_0'].split()[-1]),
                                 b'x-access-token:mirror-synthetic')
                self.assertTrue(all(not path.exists() for path in key_paths))

    def test_refused_catalog_push_still_mirrors_main_and_reports_failure(self):
        for deploy_key in ('synthetic-private-key', None):
            with self.subTest(deploy_key=bool(deploy_key)), tempfile.TemporaryDirectory(prefix='hub-refusal-') as tmp:
                root, pushes, keys = Path(tmp), [], []
                def transport(args, directory, *, env):
                    if args[0] == 'push':
                        pushes.append(args)
                        if 'razzant/OuroborosHub.git' in args[1]:
                            if deploy_key:
                                command = shlex.split(env['GIT_SSH_COMMAND'])
                                keys.append(Path(command[command.index('-i') + 1]))
                            raise ValueError('synthetic ruleset refusal')
                    return 'main' if args == ['branch', '--show-current'] else ''
                with patch.dict(os.environ, {'RUNNER_TEMP': tmp}), \
                        patch.object(sync_mirror, 'run_git', side_effect=transport), \
                        patch.object(sync_mirror, 'build_catalog', return_value={'skills': []}), \
                        self.assertRaisesRegex(ValueError, 'mirrored main'):
                    sync_mirror.sync(root, 'repo-synthetic', 'mirror-synthetic', deploy_key)
                self.assertEqual(len(pushes), 2)
                self.assertEqual(pushes[-1], ['push', 'https://github.com/joi-lab/OuroborosHub.git', 'HEAD^:refs/heads/main'])
                self.assertTrue(all(not path.exists() for path in keys))

    def test_pinned_ed25519_key_matches_github_published_fingerprint(self):
        host, kind, encoded = sync_mirror.GITHUB_HOST_KEY.split()
        self.assertEqual((host, kind), ('github.com', 'ssh-ed25519'))
        fingerprint = base64.b64encode(hashlib.sha256(base64.b64decode(encoded)).digest()).decode().rstrip('=')
        self.assertEqual(fingerprint, '+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU')

    def test_git_auth_is_in_child_environment_only(self):
        with patch.object(sync_mirror, 'run_git', return_value='done') as run:
            self.assertEqual(sync_mirror.git(ROOT, ['push', 'https://github.com/a/b.git'], 'synthetic'), 'done')
        args, kwargs = run.call_args
        self.assertNotIn('synthetic', str(args))
        env = kwargs['env']
        self.assertEqual(env['GIT_CONFIG_KEY_0'], 'http.https://github.com/.extraheader')
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], 'AUTHORIZATION: basic ' + base64.b64encode(b'x-access-token:synthetic').decode())


class GitHubAPITests(unittest.TestCase):
    def test_pagination_and_contents(self):
        api = GitHub('synthetic')
        with patch.object(api, 'request', side_effect=[{'items': list(range(100))}, {'items': [100]}]) as request:
            self.assertEqual(list(api.pages('/items?filter=latest', 'items')), list(range(101)))
            self.assertIn('&per_page=100&page=2', request.call_args.args[1])
        data = base64.b64encode(b'{"owner":"author"}').decode()
        with patch.object(api, 'request', return_value={'type': 'file', 'encoding': 'base64', 'content': data}):
            self.assertEqual(api.contents('a/b', 'entries/demo.json', 'c' * 40), {'owner': 'author'})
        with patch.object(api, 'request', return_value={'type': 'symlink'}), self.assertRaises(ValueError):
            api.contents('a/b', 'entries/demo.json', 'main')

    def test_errors_do_not_echo_response_bodies_or_tokens(self):
        api = GitHub('synthetic-secret')
        api.opener = Mock()
        api.opener.open.side_effect = HTTPError('https://api.github.com/graphql', 403, 'synthetic-secret', {}, io.BytesIO(b'synthetic-secret'))
        with self.assertRaises(APIError) as raised:
            api.request('GET', '/repos/a/b')
        self.assertNotIn('synthetic-secret', str(raised.exception))
        with self.assertRaises(ValueError):
            api.request('GET', 'https://other.example/')
        with patch.object(api, 'request', return_value={'errors': [{'message': 'synthetic-secret'}]}), self.assertRaises(RuntimeError) as raised:
            api.graphql('query {}', {})
        self.assertNotIn('synthetic-secret', str(raised.exception))
        limited = {'errors': [{'type': 'RATE_LIMITED', 'message': 'synthetic-secret'}]}
        with patch.object(api, 'request', return_value=limited), self.assertRaises(RuntimeError) as raised:
            api.graphql('query {}', {})
        self.assertIn('RATE_LIMITED', str(raised.exception))  # the error type, never its message
        self.assertNotIn('synthetic-secret', str(raised.exception))

    def test_signed_artifact_redirect_drops_authorization(self):
        request = Request('https://api.github.com/artifact', headers={'Authorization': 'Bearer synthetic'})
        redirect = SafeRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://blob.example/archive')
        self.assertFalse(redirect.has_header('Authorization'))
        downgraded = SafeRedirect().redirect_request(request, None, 302, 'Found', {}, 'http://api.github.com/new')
        self.assertFalse(downgraded.has_header('Authorization'))
        same = SafeRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://api.github.com/new')
        self.assertTrue(same.has_header('Authorization'))


class WorkflowTests(unittest.TestCase):
    def workflow(self, name):
        # BaseLoader keeps YAML's `on` key as text (YAML 1.1 SafeLoader treats it as a boolean).
        return yaml.load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)

    def test_validation_is_read_only_and_keeps_every_existing_suite(self):
        workflow = self.workflow('catalog-check.yml')
        self.assertIn('pull_request', workflow['on'])
        self.assertNotIn('pull_request_target', workflow['on'])
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(set(workflow['jobs']), {'catalog', 'hosted-skills', 'checks'})
        catalog = workflow['jobs']['catalog']['steps']
        commands = '\n'.join(s.get('run', '') for s in catalog)
        self.assertLess(commands.index('scripts/ci_checks.py'), commands.index('scripts/build_index.py'))
        for marker in ('scripts/build_index.py --check', 'tests/test_catalog.py', 'tests/test_index.py',
                       'tests/test_ci_checks.py', 'owner_updates.py verdict'):
            self.assertIn(marker, commands)
        self.assertNotIn('unittest discover', commands)
        self.assertNotIn('skills/', commands)
        self.assertTrue(any(s.get('uses') == 'actions/upload-artifact@v4' for s in catalog))
        artifact = next(s for s in catalog if s.get('uses') == 'actions/upload-artifact@v4')
        self.assertEqual(artifact['with']['overwrite'], 'true')  # Full reruns replace the prior attempt's verdict.
        hosted = workflow['jobs']['hosted-skills']
        self.assertEqual(hosted['needs'], 'catalog')
        self.assertEqual(hosted['if'], "needs.catalog.outputs.suites != '[]'")
        self.assertEqual(hosted['strategy'], {'fail-fast': 'false', 'matrix': {
            'suite': '${{ fromJSON(needs.catalog.outputs.suites) }}'}})
        self.assertEqual(workflow['jobs']['catalog']['outputs'], {'suites': '${{ steps.prepare.outputs.suites }}'})
        self.assertEqual(hosted['steps'][-1]['run'], 'python -B scripts/ci_checks.py run "$SUITE"')
        node_setup = next(s for s in hosted['steps'] if s.get('uses') == 'actions/setup-node@v4')
        self.assertEqual(node_setup['if'], "matrix.suite == 'chrome-browser'")
        self.assertEqual(node_setup['with']['node-version'], '20')
        self.assertIn('tests/test_chrome_browser.py', ci_checks.SUITES['chrome-browser']['tests'])
        self.assertIn('skills/slack-bridge/tests', ci_checks.SUITES['slack-bridge']['tests'])
        self.assertIn('skills/telegram-bot/tests', ci_checks.SUITES['telegram-bot']['tests'])
        for path in (ROOT / 'tests').glob('test_*.py'):
            relative = path.relative_to(ROOT).as_posix()
            self.assertTrue(relative in commands or any(relative in suite['tests'] + suite.get('shared', [])
                                                        for suite in ci_checks.SUITES.values()), relative)
        self.assertNotIn('secrets.', commands)

    def test_aggregate_runs_after_failures_and_accepts_only_success_or_skipped(self):
        job = self.workflow('catalog-check.yml')['jobs']['checks']
        self.assertEqual(set(job['needs']), {'catalog', 'hosted-skills'})
        self.assertEqual(job['if'], '${{ always() }}')
        step = job['steps'][0]
        self.assertEqual(step['env'], {'NEEDS': '${{ toJSON(needs) }}'})
        script = step['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for results in itertools.product(('success', 'skipped', 'failure', 'cancelled'), repeat=2):
            with self.subTest(results=results):
                needs = {name: {'result': result} for name, result in zip(job['needs'], results)}
                run = subprocess.run([sys.executable, '-B', '-c', script], timeout=30,
                                     env={**os.environ, 'NEEDS': json.dumps(needs)}, capture_output=True)
                self.assertEqual(run.returncode, 0 if all(r in {'success', 'skipped'} for r in results) else 1)

    def test_owner_write_job_uses_only_main_helpers_and_data_artifacts(self):
        workflow = self.workflow('owner-updates.yml')
        self.assertEqual(workflow['on'], {'workflow_run': {'workflows': ['Validate Catalog'], 'types': ['completed']}})
        job = workflow['jobs']['merge']
        self.assertEqual(job['permissions'], {'actions': 'read', 'contents': 'write', 'pull-requests': 'write'})
        self.assertEqual(job['steps'][0]['with'], {'ref': 'main', 'persist-credentials': 'false'})
        commands = '\n'.join(s.get('run', '') for s in job['steps'])
        self.assertEqual(commands.strip(), 'python -I scripts/owner_updates.py merge')
        self.assertNotIn('head_sha', commands)
        self.assertNotIn('pip install', commands)

    def test_daily_signals_and_explicit_catalog_mirror_sequence(self):
        signals = self.workflow('signals.yml')
        self.assertIn('workflow_dispatch', signals['on'])
        self.assertEqual(signals['on']['schedule'][0]['cron'].split()[2:], ['*', '*', '*'])
        self.assertEqual(signals['permissions'], {'contents': 'write', 'discussions': 'write'})
        self.assertEqual({s['uses'] for s in signals['jobs']['snapshot']['steps'] if 'uses' in s},
                         {'actions/checkout@v4', 'actions/setup-python@v5'})
        self.assertEqual(signals['concurrency']['cancel-in-progress'], 'false')
        mirror = self.workflow('sync-joi-lab.yml')
        self.assertEqual(mirror['on']['workflow_run']['workflows'], ['Owner Updates'])
        self.assertEqual(mirror['on']['push']['branches'], ['main'])
        self.assertEqual(mirror['jobs']['sync']['steps'][0]['with']['ref'], 'main')
        self.assertEqual(mirror['jobs']['sync']['steps'][-1]['run'], 'python -B scripts/sync_mirror.py')
        self.assertEqual(mirror['jobs']['sync']['steps'][-1]['env']['CATALOG_DEPLOY_KEY'], '${{ secrets.CATALOG_DEPLOY_KEY }}')
