"""Publication sequencing, API hygiene and workflow privilege separation."""
from __future__ import annotations

import base64
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import re
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

from scripts import ci_checks, owner_updates, sync_mirror
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


CORE_REPOSITORY = 'razzant/ouroboros'
CORE_REF = '${{ env.OUROBOROS_CORE_SHA }}'
SIGN_IN_CORE_REF = '${{ env.OUROBOROS_SIGN_IN_CORE_SHA }}'
SIGN_IN_CORE_PATH = 'sign-in-core'
CALENDAR_SIGN_IN_IF = "matrix.suite == 'calendar-skill'"
# razzant/ouroboros as a whole name inside a command or URL (never razzant/OuroborosHub).
CORE_NAMED = re.compile(r'(?<![\w.-])razzant/ouroboros(?:\.git)?(?![\w.-])', re.IGNORECASE)
# The git commands that move or fill a checkout.
GIT_MOVES = {'checkout', 'switch', 'reset', 'restore', 'fetch', 'pull', 'clone', 'worktree', 'submodule'}


def workflow_documents(directory):
    """Every workflow GitHub loads from the directory (`.yml` and `.yaml`), by file name, as BaseLoader documents."""
    return {path.name: yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
            for path in sorted(directory.iterdir()) if path.suffix in {'.yml', '.yaml'} and path.is_file()}


def is_core_repository(value):
    """razzant/ouroboros as a checkout names it: any case, an owner/name, a GitHub URL, a .git suffix."""
    text = re.sub(r'^(?:https?://|git@)?(?:www\.)?github\.com[/:]', '', str(value).strip().lower()).rstrip('/')
    return text.removesuffix('.git') == CORE_REPOSITORY


def core_git_commands(script):
    """The git commands of a run step that fetch or move a core: one naming razzant/ouroboros, or one run in core/."""
    found = []
    for command in re.split(r'\n|;|&&|\|\|', script):
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        at = next((index for index, word in enumerate(words) if word.rsplit('/', 1)[-1] == 'git'), None)
        if at is None or not GIT_MOVES & set(words):
            continue
        args = words[at + 1:]
        in_core = any(flag == '-C' and target.rstrip('/').rsplit('/', 1)[-1] in {'core', SIGN_IN_CORE_PATH}
                      for flag, target in zip(args, args[1:]))
        if in_core or CORE_NAMED.search(command):
            found.append(command.strip())
    return found


def core_pin_violations(workflows):
    """Every way these workflows (file name -> BaseLoader document) test against a core other than the one pin
    (plus the single temporary Calendar service sign-in checkout): catalog-check.yml declares both commits once,
    every checkout of the core reads its allowed pin, and no run step fetches or moves a core with git."""
    top = workflows['catalog-check.yml'].get('env', {})
    pin, sign_in_pin = top.get('OUROBOROS_CORE_SHA', ''), top.get('OUROBOROS_SIGN_IN_CORE_SHA', '')
    found = [] if re.fullmatch('[0-9a-f]{40}', pin) else [f'OUROBOROS_CORE_SHA is not one full commit: {pin!r}']
    if not re.fullmatch('[0-9a-f]{40}', sign_in_pin):
        found.append(f'OUROBOROS_SIGN_IN_CORE_SHA is not one full commit: {sign_in_pin!r}')
    elif sign_in_pin == pin:
        found.append('OUROBOROS_SIGN_IN_CORE_SHA matches OUROBOROS_CORE_SHA; remove the temporary second pin')
    sign_in_checkouts = 0
    for file, workflow in workflows.items():
        scopes = [] if file == 'catalog-check.yml' else [(file, workflow.get('env', {}))]
        for job_name, job in workflow.get('jobs', {}).items():
            scopes.append((f'{file} job {job_name}', job.get('env', {})))
            for number, step in enumerate(job.get('steps', []), 1):
                where = f'{file} job {job_name} step {number}'
                scopes.append((where, step.get('env', {})))
                options = step.get('with', {})
                if step.get('uses', '').startswith('actions/checkout@') and is_core_repository(options.get('repository', '')):
                    calendar_sign_in = (
                        file == 'catalog-check.yml' and job_name == 'hosted-skills'
                        and step.get('if') == CALENDAR_SIGN_IN_IF and options.get('repository') == CORE_REPOSITORY
                        and options.get('ref') == SIGN_IN_CORE_REF and options.get('path') == SIGN_IN_CORE_PATH
                    )
                    if file != 'catalog-check.yml':  # where the pin is undefined: the checkout would follow a branch
                        found.append(f'{where} checks out the core outside catalog-check.yml, which holds its pin')
                    elif calendar_sign_in:
                        sign_in_checkouts += 1
                        if sign_in_checkouts > 1:
                            found.append(f'{where} duplicates the Calendar sign-in core checkout')
                    elif options.get('ref') != CORE_REF or options.get('repository') != CORE_REPOSITORY:
                        found.append(f'{where} checks out {options.get("repository")!r} at {options.get("ref")!r}, '
                                     f'not {CORE_REPOSITORY} at {CORE_REF}')
                found += [f'{where} fetches or moves a core: {command}' for command in core_git_commands(step.get('run', ''))]
        for key in ('OUROBOROS_CORE_SHA', 'OUROBOROS_SIGN_IN_CORE_SHA'):
            found += [f'{where} declares {key} again' for where, env in scopes if key in env]
    return found


class WorkflowTests(unittest.TestCase):
    def workflow(self, name):
        # BaseLoader keeps YAML's `on` key as text (YAML 1.1 SafeLoader treats it as a boolean).
        return yaml.load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)

    def test_validation_is_read_only_and_keeps_every_existing_suite(self):
        workflow = self.workflow('catalog-check.yml')
        # Draft then Ready starts a fresh validation of a new tested merge; a rerun repeats the old one.
        self.assertEqual(workflow['on']['pull_request'],
                         {'types': ['opened', 'synchronize', 'reopened', 'ready_for_review']})
        self.assertNotIn('pull_request_target', workflow['on'])
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(set(workflow['jobs']), {'catalog', 'hosted-skills', 'checks', 'receipt'})
        # Owner Updates expects these fixed job names plus one hosted job named by each selected suite.
        self.assertEqual(set(workflow['jobs']) - {'hosted-skills'}, owner_updates.REQUIRED_JOBS)
        self.assertEqual(workflow['jobs']['hosted-skills']['name'], '${{ matrix.suite }}')
        catalog = workflow['jobs']['catalog']['steps']
        commands = '\n'.join(s.get('run', '') for s in catalog)
        self.assertLess(commands.index('scripts/ci_checks.py'), commands.index('scripts/build_index.py'))
        for marker in ('scripts/build_index.py --check', 'tests/test_catalog.py', 'tests/test_index.py',
                       'tests/test_ci_checks.py', 'tests/test_owner_updates.py'):
            self.assertIn(marker, commands)
        self.assertNotIn('unittest discover', commands)
        self.assertNotIn('skills/', commands)
        # Eligibility is decided only by the trusted job: validation never runs the helper itself.
        self.assertNotIn('scripts/owner_updates.py', commands)
        self.assertNotIn('owner_updates.py verdict', commands)
        # Every checkout of this repository is the one commit the run tests; the core is checked out only at its one
        # pin (test_the_core_is_checked_out_only_at_the_one_pin).
        hosted = workflow['jobs']['hosted-skills']
        for steps in (catalog, hosted['steps']):
            own = [s for s in steps if s.get('uses') == 'actions/checkout@v4' and 'repository' not in s.get('with', {})]
            self.assertEqual([s['with']['ref'] for s in own], ['${{ github.sha }}'])
        receipt = workflow['jobs']['receipt']
        self.assertEqual(receipt['needs'], 'checks')
        self.assertEqual(receipt['if'], "${{ !cancelled() && github.event_name == 'pull_request' && "
                                        "needs.checks.result == 'success' }}")
        self.assertFalse(any(s.get('uses', '').startswith(('actions/checkout', 'actions/setup')) for s in receipt['steps']))
        self.assertEqual(receipt['steps'][0]['env']['TESTED_SHA'], '${{ github.sha }}')
        self.assertNotIn('${{', receipt['steps'][0]['run'])  # Contexts reach the script only through env.
        upload = receipt['steps'][1]
        self.assertEqual((upload['uses'], upload['with']['name'], upload['with']['overwrite']),
                         ('actions/upload-artifact@v4', 'owner-update-receipt-${{ github.run_id }}-${{ github.run_attempt }}',
                          'false'))
        self.assertEqual(len(receipt['steps']), 2)
        self.assertEqual(hosted['needs'], 'catalog')
        self.assertEqual(hosted['if'], "needs.catalog.outputs.suites != '[]'")
        self.assertEqual(hosted['strategy'], {'fail-fast': 'false', 'matrix': {
            'suite': '${{ fromJSON(needs.catalog.outputs.suites) }}'}})
        self.assertEqual(workflow['jobs']['catalog']['outputs'], {'suites': '${{ steps.prepare.outputs.suites }}'})
        self.assertEqual(hosted['steps'][-1]['run'], 'python -B scripts/ci_checks.py run "$SUITE"')
        self.assertIn('skills/google-workspace/tests', ci_checks.SUITES['google-workspace']['tests'])
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

    def test_the_core_is_checked_out_only_at_the_one_pin(self):
        directory = ROOT / '.github/workflows'
        workflows = workflow_documents(directory)
        self.assertEqual(set(workflows), {path.name for path in directory.glob('*.y*ml')})
        self.assertEqual(core_pin_violations(workflows), [])
        pin = workflows['catalog-check.yml']['env']['OUROBOROS_CORE_SHA']
        old = '9f6fe2f5f7f4d8f3e5ea58f0d04fb352181416bc'
        steps = lambda documents: documents['catalog-check.yml']['jobs']['hosted-skills']['steps']
        [checkout, sign_in_checkout] = [step for step in steps(workflows) if step.get('with', {}).get('repository') == CORE_REPOSITORY]
        at, sign_in_at = steps(workflows).index(checkout), steps(workflows).index(sign_in_checkout)
        second = {**checkout, 'with': {**checkout['with'], 'ref': old, 'path': 'presence-core'}}
        elsewhere = {'uses': 'actions/checkout@v4', 'with': {'repository': 'razzant/OuroborosHub-fixtures', 'ref': old}}
        # Repositories other than the core may be pinned by commit, in a checkout or a script: nothing to report.
        for label, change in [
                ('another repository at a full commit', lambda w: steps(w).insert(at + 1, elsewhere)),
                ('a script moving another checkout', lambda w: steps(w).insert(at + 1, {'run': f'git -C fixtures checkout {old}'})),
                ('a script reading the core', lambda w: steps(w).insert(at + 1, {'run': 'git -C core log -1 --format=%H'})),
        ]:
            with self.subTest(label):
                changed = json.loads(json.dumps(workflows))  # BaseLoader yields only dicts, lists and strings
                change(changed)
                self.assertEqual(core_pin_violations(changed), [])
        # A second pin, however it is spelled, fails; each change is applied to a fresh copy of the real workflows.
        for label, change in [
                ('a second checkout at an older commit', lambda w: steps(w).insert(at + 1, second)),
                ('the checkout at a literal copy of the pin', lambda w: steps(w)[at]['with'].update(ref=pin)),
                ('a ref that depends on the suite', lambda w: steps(w)[at]['with'].update(
                    ref=f"${{{{ matrix.suite == 'email-presence' && '{old}' || env.OUROBOROS_CORE_SHA }}}}")),
                ('the core spelled as a URL at a branch', lambda w: steps(w).insert(at + 1, {**second, 'with': {
                    **second['with'], 'repository': 'https://github.com/Razzant/Ouroboros.git', 'ref': 'ouroboros'}})),
                ('a job that declares the pin again', lambda w: w['catalog-check.yml']['jobs']['hosted-skills'].update(
                    env={'OUROBOROS_CORE_SHA': old})),
                ('a checkout of the core in another workflow', lambda w: w['signals.yml']['jobs']['snapshot']['steps'].append(second)),
                ('another workflow reading a pin it cannot see', lambda w: w['signals.yml']['jobs']['snapshot']['steps'].append(
                    checkout)),
                ('a script moving the pinned checkout', lambda w: steps(w).insert(at + 1, {'run': f'git -C core checkout {old}'})),
                ('a script fetching a branch into it', lambda w: steps(w).insert(at + 1, {
                    'run': 'cd "$GITHUB_WORKSPACE"\ngit -C "$GITHUB_WORKSPACE/core" fetch origin ouroboros'})),
                ('a script cloning the core', lambda w: steps(w).insert(at + 1, {
                    'run': 'git clone --depth 1 https://github.com/razzant/ouroboros.git core2'})),
                ('a pin that is not a full commit', lambda w: w['catalog-check.yml']['env'].update(OUROBOROS_CORE_SHA=pin[:12])),
                ('the Calendar sign-in pin is not a full commit', lambda w: w['catalog-check.yml']['env'].update(OUROBOROS_SIGN_IN_CORE_SHA=pin[:12])),
                ('the Calendar sign-in pin duplicates the main pin', lambda w: w['catalog-check.yml']['env'].update(OUROBOROS_SIGN_IN_CORE_SHA=pin)),
                ('a job that declares the Calendar sign-in pin again', lambda w: w['catalog-check.yml']['jobs']['hosted-skills'].update(env={'OUROBOROS_SIGN_IN_CORE_SHA': old})),
                ('the Calendar sign-in checkout widened to another suite', lambda w: steps(w)[sign_in_at].update({'if': "matrix.suite == 'email-presence'"})),
                ('the Calendar sign-in checkout overwriting core/', lambda w: steps(w)[sign_in_at]['with'].update(path='core')),
                ('a duplicate Calendar sign-in checkout', lambda w: steps(w).insert(sign_in_at + 1, dict(sign_in_checkout))),
                ('a script moving the Calendar sign-in checkout', lambda w: steps(w).insert(sign_in_at + 1, {'run': f'git -C {SIGN_IN_CORE_PATH} checkout {old}'})),
        ]:
            with self.subTest(label):
                changed = json.loads(json.dumps(workflows))
                change(changed)
                self.assertNotEqual(core_pin_violations(changed), [])
        # GitHub loads `.yaml` workflows too: discovery finds one, and its own core checkout fails the check.
        with tempfile.TemporaryDirectory(prefix='hub-workflows-') as tmp:
            copy = Path(tmp)
            for path in directory.iterdir():
                (copy / path.name).write_bytes(path.read_bytes())
            consumer = {'on': {'push': ''}, 'jobs': {'consumer': {'runs-on': 'ubuntu-latest', 'steps': [second]}}}
            (copy / 'consumer.yaml').write_text(yaml.safe_dump(consumer), encoding='utf-8')
            found = workflow_documents(copy)
            self.assertIn('consumer.yaml', found)
            self.assertTrue(any(line.startswith('consumer.yaml job consumer step 1') for line in core_pin_violations(found)))
            (copy / 'consumer.yaml').write_text(yaml.safe_dump({**consumer, 'jobs': {'consumer': {
                'runs-on': 'ubuntu-latest', 'steps': [elsewhere]}}}), encoding='utf-8')
            self.assertEqual(core_pin_violations(workflow_documents(copy)), [])

    def test_every_suite_that_needs_a_core_runs_on_the_one_checkout(self):
        workflow = self.workflow('catalog-check.yml')
        # The catalog job compares every hosted release and connector with that core (tests/test_index.py).
        catalog = workflow['jobs']['catalog']['steps']
        [core] = [step for step in catalog if step.get('with', {}).get('repository') == CORE_REPOSITORY]
        self.assertEqual(core['with'], {'repository': CORE_REPOSITORY, 'ref': CORE_REF, 'path': 'core',
                                        'persist-credentials': 'false'})
        tests = next(step for step in catalog if 'tests/test_index.py' in step.get('run', ''))
        self.assertLess(catalog.index(core), catalog.index(tests))
        self.assertEqual(tests['env'], {'OUROBOROS_IDENTITY_CORE_ROOT': '${{ github.workspace }}/core'})
        hosted = workflow['jobs']['hosted-skills']
        steps = hosted['steps']
        needs_core = sorted(name for name, spec in ci_checks.SUITES.items() if ci_checks.core_envs(spec))

        def selecting(suites):
            return f"contains(fromJSON('{json.dumps(sorted(suites))}'), matrix.suite)"
        [checkout, sign_in_checkout] = [step for step in steps if step.get('with', {}).get('repository') == CORE_REPOSITORY]
        self.assertEqual(checkout['if'], selecting(needs_core))
        # The dashboard suite reads the core's historical bridge (tests/test_dashboard_theme_browser.py LEGACY_REF).
        self.assertEqual(checkout['with'], {'repository': CORE_REPOSITORY, 'ref': CORE_REF, 'path': 'core',
                                            'persist-credentials': 'false',
                                            'fetch-depth': "${{ matrix.suite == 'widgets' && '0' || '1' }}"})
        self.assertIn('LEGACY_REF = "', (ROOT / 'tests/test_dashboard_theme_browser.py').read_text(encoding='utf-8'))
        install = next(step for step in steps if step.get('name') == "Install the core's runtime and the browsers, and name the core")
        workspace_runtime = next(step for step in steps if step.get('name') == "Install the core's runtime and name the core for Google Workspace")
        source = next(step for step in steps if step.get('name') == 'Name the core for the Presence consumers and GitHub')
        # The suites whose tests drive browsers through the core install its runtime and both browsers, and get 30
        # minutes; the Presence consumers and GitHub read its source.
        runtime = ['calendar-skill', 'email-presence', 'memory-atlas', 'smart-lists', 'telegram-user-session', 'widgets']
        consumer_suites = [name for name in needs_core if 'OUROBOROS_PRESENCE_CORE_ROOT' in ci_checks.core_envs(ci_checks.SUITES[name])]
        readers = sorted(consumer_suites + ['github'])
        self.assertEqual((install['if'], source['if']), (selecting(runtime), selecting(readers)))
        self.assertEqual(workspace_runtime['if'], "matrix.suite == 'google-workspace'")
        self.assertEqual(sorted(set(runtime) | set(readers) | {'google-workspace'}), needs_core)
        longer = re.fullmatch(r"\$\{\{ contains\(fromJSON\('(\[.*\])'\), matrix\.suite\) && 30 \|\| 15 \}\}",
                              hosted['timeout-minutes'])
        self.assertEqual(sorted(json.loads(longer.group(1))), runtime)
        # The Presence consumers (tests/test_*_core_consumer.py): every suite that lists one requires the core.
        consumers = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'tests').glob('test_*_core_consumer.py')
                     if 'CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"' in path.read_text(encoding='utf-8')}
        self.assertEqual(consumers, {'tests/test_slack_conversation_core_consumer.py', 'tests/test_reaction_core_consumer.py',
                                     'tests/test_gesture_core_consumer.py', 'tests/test_voice_attachment_core_consumer.py',
                                     'tests/test_telegram_bot_media_core_consumer.py',
                                     'tests/test_telegram_bot_typing_core_consumer.py',
                                     'tests/test_freshdesk_core_consumer.py'})
        self.assertEqual([suite for suite, spec in ci_checks.SUITES.items() if consumers & set(spec['tests'])],
                         [suite for suite in ci_checks.SUITES if suite in consumer_suites])
        python = next(step for step in steps if step.get('uses') == 'actions/setup-python@v5')
        self.assertEqual(python['with']['python-version'],
                         '${{ contains(fromJSON(\'["memory-atlas", "calendar-skill", "email-presence"]\'), matrix.suite) && \'3.10\' || \'3.11\' }}')
        # Execute both shell steps as written on either runner; substitute only the downloads. Each names the one
        # checkout, and every suite is given the variable its tests read (core_env) by the step that runs for it.
        named = {}
        for step, expected in [(install, ['-m pip install -r core/requirements.txt', '-m playwright install {}chromium webkit']),
                               (workspace_runtime, ['-m pip install -r core/requirements.txt']),
                               (source, [])]:
            for runner in ('Linux', 'macOS'):
                with self.subTest(step=step['name'], runner=runner), tempfile.TemporaryDirectory(prefix='hub core workflow ') as tmp:
                    root = Path(tmp)
                    prelude = 'python() { printf "%s\\n" "$*" >> "$PROBES"; }\n'
                    subprocess.run(['/bin/sh', '-c', prelude + step['run']], check=True, capture_output=True, text=True,
                                   env={**os.environ, 'RUNNER_OS': runner, 'GITHUB_WORKSPACE': str(root),
                                        'GITHUB_ENV': str(root / 'environment'), 'PROBES': str(root / 'probes')})
                    env = dict(line.split('=', 1) for line in (root / 'environment').read_text(encoding='utf-8').splitlines())
                    self.assertEqual(set(env.values()), {str(root / 'core')})
                    named[step['name']] = set(env)
                    probes = (root / 'probes').read_text(encoding='utf-8').splitlines() if expected else []
                    deps = '--with-deps ' if runner == 'Linux' else ''  # Linux runners also need the browsers' libraries
                    self.assertEqual(probes, [line.format(deps) for line in expected])
        # Each runtime suite also gets the opt-in names its tests read (smart-lists' core validator: OUROBOROS_CORE_ROOT).
        self.assertEqual(named[install['name']], {'ATLAS_CORE', 'OUROBOROS_SOURCE_DIR', 'OUROBOROS_THEME_CORE_ROOT',
                                                  'OUROBOROS_CORE_ROOT'})
        self.assertEqual(named[workspace_runtime['name']], {'OUROBOROS_SOURCE_DIR'})
        self.assertEqual(named[source['name']], {'OUROBOROS_PRESENCE_CORE_ROOT', 'OUROBOROS_IDENTITY_CORE_ROOT'})
        for suite in needs_core:
            given = set().union(*(named[step['name']] for step, chosen in ((install, runtime), (source, readers), (workspace_runtime, ['google-workspace']))
                                  if suite in chosen))
            self.assertLessEqual(set(ci_checks.core_envs(ci_checks.SUITES[suite])), given, suite)
        for step in (install, workspace_runtime, source):
            self.assertLess(steps.index(checkout), steps.index(step))
            self.assertLess(steps.index(step), len(steps) - 1)  # it precedes the suite run
        self.assertEqual(sign_in_checkout['if'], CALENDAR_SIGN_IN_IF)
        self.assertEqual(sign_in_checkout['with'], {'repository': CORE_REPOSITORY, 'ref': SIGN_IN_CORE_REF,
                                                    'path': SIGN_IN_CORE_PATH, 'persist-credentials': 'false',
                                                    'fetch-depth': '1'})
        sign_in_run = next(step for step in steps if step.get('name') == 'Run Calendar service sign-in check')
        self.assertEqual(sign_in_run['if'], CALENDAR_SIGN_IN_IF)
        self.assertEqual(sign_in_run['env'], {'OUROBOROS_SIGN_IN_CORE_ROOT': f'${{{{ github.workspace }}}}/{SIGN_IN_CORE_PATH}'})
        self.assertEqual(sign_in_run['run'],
                         'python -B -m pytest -p no:cacheprovider -v -rs tests/calendar_sign_in/test_host_sign_in_core.py')
        self.assertLess(steps.index(install), steps.index(sign_in_checkout))
        self.assertLess(steps.index(sign_in_checkout), steps.index(sign_in_run))
        self.assertLess(steps.index(sign_in_run), len(steps) - 1)
        self.assertIn('tests/calendar_sign_in/test_host_sign_in_core.py', ci_checks.SUITES['calendar-skill']['shared'])

    def test_chrome_browser_workflow_installs_node_and_the_npm_closure_its_pins_record(self):
        hosted = self.workflow('catalog-check.yml')['jobs']['hosted-skills']
        node = next(step for step in hosted['steps'] if step.get('uses') == 'actions/setup-node@v4')
        install = next(step for step in hosted['steps'] if step.get('name') == 'Install the npm closure that chrome_browser pins')
        self.assertEqual(node['if'], "matrix.suite == 'chrome-browser' || matrix.suite == 'memory-atlas'")
        self.assertEqual(install['if'], "matrix.suite == 'chrome-browser'")
        self.assertEqual(node['with'], {'node-version': '24'})
        self.assertEqual(ci_checks.SUITES['chrome-browser'], {'skills': ['chrome_browser'], 'tests': ['tests/chrome_browser']})
        steps = hosted['steps']
        self.assertLess(steps.index(node), steps.index(install))
        self.assertLess(steps.index(install), len(steps) - 1)  # both precede the suite run
        self.assertEqual(steps[-1]['run'], 'python -B scripts/ci_checks.py run "$SUITE"')
        version = json.loads((ROOT / 'skills/chrome_browser/pins.json').read_text(encoding='utf-8'))['npm']['@playwright/mcp']['version']
        with tempfile.TemporaryDirectory(prefix='hub chrome workflow ') as tmp:
            root = Path(tmp)
            output = root / 'environment'
            # Execute the actual shell step; substitute only the download (one argument per line shows its quoting).
            # Python is the real one, so the version comes from the real pins file.
            prelude = f'python() {{ {shlex.quote(sys.executable)} "$@"; }}\nnpm() {{ printf "%s\\n" "$@" >> "$PROBES"; }}\n'
            subprocess.run(['/bin/sh', '-c', prelude + install['run']], check=True, cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, 'RUNNER_TEMP': str(root), 'GITHUB_ENV': str(output), 'PROBES': str(root / 'probes')})
            closure = root / 'chrome-browser-npm'
            self.assertEqual(output.read_text(encoding='utf-8'), f'OUROBOROS_CHROME_NODE_MODULES={closure / "node_modules"}\n')
            self.assertEqual((root / 'probes').read_text(encoding='utf-8').splitlines(),
                             ['install', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix', str(closure),
                              f'@playwright/mcp@{version}'])

    def test_owner_write_job_uses_only_main_helpers_and_data_artifacts(self):
        workflow = self.workflow('owner-updates.yml')
        self.assertEqual(workflow['on']['workflow_run'], {'workflows': ['Validate Catalog'], 'types': ['completed']})
        self.assertEqual(workflow['on']['workflow_dispatch'], {'inputs': {'pr': {
            'description': 'Pull request number to hold from automatic merging', 'required': 'true', 'type': 'string'}}})
        # Merges and holds are serialized in one existing group; nothing pending is cancelled or dropped.
        self.assertEqual(workflow['concurrency'], {'group': 'owner-updates', 'cancel-in-progress': 'false', 'queue': 'max'})
        self.assertEqual(set(workflow['jobs']), {'merge', 'hold'})
        job = workflow['jobs']['merge']
        self.assertIn("github.event_name == 'workflow_run'", job['if'])
        self.assertEqual(job['permissions'], {'actions': 'read', 'contents': 'write', 'pull-requests': 'write'})
        self.assertEqual(job['steps'][0]['with'], {'ref': 'main', 'fetch-depth': '0', 'persist-credentials': 'false'})
        commands = '\n'.join(s.get('run', '') for s in job['steps'])
        self.assertEqual(commands.strip(), 'python -I scripts/owner_updates.py merge')
        self.assertNotIn('head_sha', commands)
        self.assertNotIn('pip install', commands)
        hold = workflow['jobs']['hold']
        self.assertIn("github.event_name == 'workflow_dispatch'", hold['if'])
        self.assertIn("github.ref == 'refs/heads/main'", hold['if'])
        self.assertEqual(hold['permissions'], {'contents': 'read', 'issues': 'write', 'pull-requests': 'write'})
        self.assertEqual(hold['steps'][0]['with'], {'ref': 'main', 'persist-credentials': 'false'})
        step = hold['steps'][-1]
        self.assertEqual(step['run'], 'python -I scripts/owner_updates.py hold --pr "$PR_NUMBER"')
        self.assertEqual(step['env']['PR_NUMBER'], '${{ inputs.pr }}')  # Never interpolated into the shell.

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
        # A failed post-merge check or an unknown outcome may still have merged: publish current main anyway.
        self.assertEqual(mirror['jobs']['sync']['if'], "github.repository == 'razzant/OuroborosHub'")
        self.assertEqual(mirror['on']['push']['branches'], ['main'])
        self.assertEqual(mirror['jobs']['sync']['steps'][0]['with']['ref'], 'main')
        self.assertEqual(mirror['jobs']['sync']['steps'][-1]['run'], 'python -B scripts/sync_mirror.py')
        self.assertEqual(mirror['jobs']['sync']['steps'][-1]['env']['CATALOG_DEPLOY_KEY'], '${{ secrets.CATALOG_DEPLOY_KEY }}')
