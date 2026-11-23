"""Both directions of the owner-update authorization boundary: real local Git objects, a fake GitHub API."""
from __future__ import annotations

import copy
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import yaml

from scripts.github_api import APIError
from scripts import owner_updates as owner

REPO = 'razzant/OuroborosHub'
ROOT = Path(__file__).resolve().parents[1]
# Isolate every git call, the product's included, from the machine's own configuration.
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.test',
           'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.test'}
BASE_FILES = {
    'entries/demo.json': {'name': 'demo', 'owner': 'author'},
    'skills/demo/plugin.py': 'v1\n',
    'entries/other.json': {'name': 'other', 'owner': 'someone'},
    'skills/other/a.py': 'a1\n',
    'official.json': {'endorsements': []},
    'scripts/check.py': 'check = 1\n',
    '.github/workflows/catalog-check.yml': 'name: Validate Catalog\n',
    'catalog.json': '{}\n',
}
UPDATE = {'entries/demo.json': {'name': 'demo', 'owner': 'author', 'description': 'new'},
          'skills/demo/plugin.py': 'v2\n'}


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, timeout=120,
                          env={**os.environ, **GIT_ENV}).stdout.decode().strip()


class Hub:
    """A bare 'GitHub' remote, an author's work tree and the trusted checkout of main."""

    def __init__(self, tmp, base=None, history=None, update=None):
        self.tmp = Path(tmp)
        self.remote, self.work, self.trusted = self.tmp / 'remote.git', self.tmp / 'work', self.tmp / 'trusted'
        self.tmp.mkdir(parents=True)
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', str(self.remote))
        git(self.remote, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        git(self.tmp, 'init', '-q', '-b', 'main', str(self.work))
        self.base = self.commit({**BASE_FILES, **(base or {})}, 'admit')
        self.push('HEAD', 'refs/heads/main')
        git(self.work, 'checkout', '-q', '-b', 'update')
        for files in (history or []) + [UPDATE if update is None else update]:
            self.head = self.commit(files, 'update')
        self.push('HEAD', 'refs/pull/12/head')
        self.tested = self.test_merge()
        git(self.tmp, 'clone', '-q', str(self.remote), str(self.trusted))

    def write(self, files):
        gitlinks = []
        for path, content in files.items():
            target = self.work / path
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, tuple) and content[0] == 'symlink':
                target.unlink(missing_ok=True)
                os.symlink(content[1], target)
            elif isinstance(content, tuple) and content[0] == 'gitlink':
                gitlinks.append((path, content[1]))
            elif isinstance(content, tuple) and content[0] == 'exec':
                target.write_text(content[1], encoding='utf-8')
                target.chmod(0o755)
            else:
                target.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
        git(self.work, 'add', '-A')  # One staging call, so thousands of files stay cheap.
        for path, sha in gitlinks:
            git(self.work, 'update-index', '--add', '--cacheinfo', f'160000,{sha},{path}')

    def commit(self, files, message):
        self.write(files)
        git(self.work, 'commit', '-q', '--allow-empty', '-m', message)
        return git(self.work, 'rev-parse', 'HEAD')

    def push(self, source, ref):
        git(self.work, 'push', '-q', '--force', str(self.remote), f'{source}:{ref}')

    def main(self):
        return git(self.remote, 'rev-parse', 'refs/heads/main')

    def test_merge(self, change=None):
        """GitHub's test merge: current main merged with the head, optionally altered."""
        git(self.work, 'fetch', '-q', str(self.remote), '+refs/heads/main:refs/remotes/hub/main')
        git(self.work, 'checkout', '-q', '--detach', 'refs/remotes/hub/main')
        git(self.work, 'merge', '-q', '--no-ff', '--no-commit', self.head)
        if change:
            self.write(change)
        git(self.work, 'commit', '-q', '-m', 'test merge')
        tested = git(self.work, 'rev-parse', 'HEAD')
        self.push(tested, f'refs/tested/{tested}')
        git(self.work, 'checkout', '-q', 'update')
        return tested

    def advance_main(self, files):
        git(self.work, 'fetch', '-q', str(self.remote), '+refs/heads/main:refs/remotes/hub/main')
        git(self.work, 'checkout', '-q', '--detach', 'refs/remotes/hub/main')
        sha = self.commit(files, 'main moves')
        self.push(sha, 'refs/heads/main')
        git(self.work, 'checkout', '-q', 'update')
        return sha

    def merge_on_remote(self, head):
        git(self.work, 'fetch', '-q', str(self.remote), '+refs/heads/main:refs/remotes/hub/main')
        git(self.work, 'checkout', '-q', '--detach', 'refs/remotes/hub/main')
        git(self.work, 'merge', '-q', '--no-ff', '-m', 'Merge owner update', head)
        sha = git(self.work, 'rev-parse', 'HEAD')
        self.push(sha, 'refs/heads/main')
        git(self.work, 'checkout', '-q', 'update')
        return sha


def archive(value, filename='receipt.json'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        bundle.writestr(filename, json.dumps(value))
    return stream.getvalue()


def bot_comment(body, comment_id=500, login=owner.BOT, kind='Bot'):
    return {'id': comment_id, 'user': {'login': login, 'type': kind}, 'body': body}


class API:
    def __init__(self, hub):
        self.hub, self.calls, self.comments = hub, [], []
        self.pr = {'number': 12, 'state': 'open', 'draft': False, 'merged': False, 'labels': [],
                   'user': {'login': 'author'}, 'base': {'ref': 'main', 'repo': {'full_name': REPO}},
                   'head': {'sha': hub.head, 'repo': {'full_name': 'author/OuroborosHub'}}}
        self.run = {'id': 90, 'event': 'pull_request', 'status': 'completed', 'conclusion': 'success',
                    'path': owner.WORKFLOW, 'repository': {'full_name': REPO},
                    'head_repository': {'full_name': 'author/OuroborosHub'}, 'head_sha': hub.head,
                    'run_attempt': 1, 'pull_requests': [{'number': 12}]}
        # GitHub's shape for an empty suite selection: the matrix job appears once as a skipped placeholder.
        self.jobs = [{'name': n, 'status': 'completed', 'conclusion': 'success'} for n in sorted(owner.REQUIRED_JOBS)]
        self.jobs.append({'name': 'matrix.suite', 'status': 'completed', 'conclusion': 'skipped'})
        self.receipt = {'schema_version': 1, 'repository': REPO, 'run_id': 90, 'run_attempt': 1, 'pr_number': 12,
                        'head_sha': hub.head, 'tested_sha': hub.tested, 'workflow_sha': hub.tested,
                        'workflow_ref': f'{REPO}/{owner.WORKFLOW}@refs/pull/12/merge'}
        self.artifacts = [{'id': 100, 'name': f'{owner.RECEIPT}-90-1', 'expired': False, 'size_in_bytes': 500}]
        self.on_pr_read = None  # (read number, pr) -> None
        self.on_run_read = None  # (read number, run) -> None
        self.before_put = None
        self.put_error = None
        self.put_result = None
        self.comment_error = None
        self.comment_fate, self.late = None, []  # 'lost' or 'late' comment writes; late ones wait in self.late.
        self.archive = None

    def count(self, method, suffix):
        return sum(c[0] == method and c[1].endswith(suffix) for c in self.calls)

    @property
    def puts(self):
        return [c for c in self.calls if c[0] == 'PUT']

    def writes(self):
        return [c for c in self.calls if c[0] in {'POST', 'PATCH', 'PUT'}]

    def request(self, method, path, body=None, *, raw=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        prefix = f'/repos/{REPO}'
        if method == 'GET' and path == f'{prefix}/actions/runs/90':
            if self.on_run_read:
                self.on_run_read(self.count('GET', '/actions/runs/90'), self.run)
            return copy.deepcopy(self.run)
        if method == 'GET' and path == f'{prefix}/pulls/12':
            pr = copy.deepcopy(self.pr)
            if self.on_pr_read:
                self.on_pr_read(self.count('GET', '/pulls/12'), pr)
                self.pr = copy.deepcopy(pr)
            return pr
        if method == 'GET' and path == f'{prefix}/git/ref/heads/main':
            return {'object': {'sha': self.hub.main()}}
        if method == 'GET' and path == f'{prefix}/actions/artifacts/100/zip':
            assert raw
            return self.archive or archive(self.receipt)
        if method in {'POST', 'PATCH'} and 'comments' in path:
            if self.comment_error:
                raise self.comment_error
            def apply():
                if method == 'POST':
                    assert path == f'{prefix}/issues/12/comments'
                    self.comments.append(bot_comment(body['body'], 500 + len(self.comments)))
                    return copy.deepcopy(self.comments[-1])
                row = next(c for c in self.comments if path == f"{prefix}/issues/comments/{c['id']}")
                row['body'] = body['body']
                return copy.deepcopy(row)
            if self.comment_fate == 'late':  # The client gives up; GitHub applies the write later.
                self.late.append(apply)
                raise TimeoutError('timed out')
            result = apply()
            if self.comment_fate == 'lost':  # Applied, but the response never arrives.
                raise TimeoutError('timed out')
            return result
        if method == 'POST' and path == f'{prefix}/issues/12/labels':
            self.pr['labels'].append({'name': body['labels'][0]})
            return []
        if method == 'PUT' and path == f'{prefix}/pulls/12/merge':
            if self.before_put:
                self.before_put()
            if self.put_error:
                raise self.put_error
            if self.put_result is not None:
                return self.put_result
            sha = self.hub.merge_on_remote(body['sha'])
            self.pr.update(merged=True, state='closed', merge_commit_sha=sha)
            return {'merged': True, 'sha': sha}
        raise AssertionError((method, path))

    def pages(self, path, key=None):
        self.calls.append(('pages', path, None))
        if path == f"/repos/{REPO}/actions/runs/90/attempts/{self.run['run_attempt']}/jobs":
            return copy.deepcopy(self.jobs)  # Only the attempt-specific listing; never /jobs?filter=latest.
        if path.endswith('/actions/runs/90/artifacts'):
            return copy.deepcopy(self.artifacts)
        if path.endswith('/issues/12/comments'):
            return copy.deepcopy(self.comments)
        raise AssertionError(path)  # In particular, /pulls/N/files is never consulted.


class Case(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix='hub-owner-update-')
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def scenario(self, **kwargs):
        self.count += 1
        hub = Hub(Path(self.tmp.name) / f'case{self.count}', **kwargs)
        remote = patch.object(owner, 'remote_url', lambda repository: str(hub.remote))
        remote.start()
        self.addCleanup(remote.stop)
        return hub, API(hub)

    def merge(self, hub, api, attempt=1, run=700):
        return owner.merge_owner_update(api, {'workflow_run': {'id': 90, 'run_attempt': attempt}}, REPO,
                                        hub.trusted, (run, 1))

    def refused(self, hub, api, error=owner.Ineligible, writes=False):
        with self.assertRaises(error):
            self.merge(hub, api)
        self.assertEqual(api.puts, [])
        if not writes:
            self.assertEqual(api.writes(), [])

    def control(self, api):
        self.assertEqual(len(api.comments), 1)
        return owner.control_of(api.comments[0]['body'])

    def phases(self, api):
        return [owner.control_of(c['body'])['phase'] for c in api.comments]


class MergeTests(Case):
    def test_ordinary_owner_update_is_announced_then_merged_keeping_its_commits(self):
        hub, api = self.scenario()
        outcome = self.merge(hub, api)
        self.assertIsNone(outcome['verification_failure'])
        self.assertEqual(api.puts, [('PUT', f'/repos/{REPO}/pulls/12/merge', {
            'sha': hub.head, 'merge_method': 'merge', 'commit_title': 'Merge owner update #12: demo',
            'commit_message': owner.COMMIT_MESSAGE})])
        notice = next(i for i, c in enumerate(api.calls) if c[0] == 'POST')
        self.assertLess(notice, api.calls.index(api.puts[0]))
        self.assertIn('"phase":"attempting"', api.calls[notice][2]['body'])
        # One record for the run: its own comment, written before the request and settled once after it.
        self.assertEqual([(c[0], c[1].rsplit('/', 2)[-2:]) for c in api.calls if c[0] in {'POST', 'PATCH'}],
                         [('POST', ['12', 'comments']), ('PATCH', ['comments', '500'])])
        self.assertEqual(git(hub.remote, 'rev-list', '--parents', '-n', '1', outcome['merge_commit']).split()[1:],
                         [hub.base, hub.head])
        control = self.control(api)
        self.assertEqual((control['phase'], control['merge_commit'], control['head'], control['commits_kept']),
                         ('merged', outcome['merge_commit'], hub.head, True))
        body = api.comments[0]['body']
        self.assertIn("keeps the contribution's commits: its parents were read back", body)
        # The CI attempt was read twice: on entry and as the last read before the request.
        self.assertEqual(api.count('GET', '/actions/runs/90'), 2)
        self.assertLess(max(i for i, c in enumerate(api.calls) if c[1].endswith('/actions/runs/90')),
                        api.calls.index(api.puts[0]))
        for phrase in ('Independent maintainer review is not required', 'does not grant Official endorsement',
                       'owner-update-hold'):
            self.assertIn(phrase, body)
        self.assertNotIn('nobody reviewed', body.casefold())
        self.assertFalse(any('/files' in c[1] for c in api.calls))
        self.assertEqual(git(hub.trusted, 'status', '--porcelain'), '')  # Nothing was checked out.

    def test_scope_has_no_file_ceiling_and_comes_from_head_objects(self):
        many = {f'skills/demo/gen/f{i:04}.txt': f'{i}\n' for i in range(3001)}
        for label, update, allowed in [('301 payload files', {**UPDATE, **dict(list(many.items())[:301])}, True),
                                       ('3001 payload files', {**UPDATE, **many}, True),
                                       ('3001 payload files and one tool', {**UPDATE, **many, 'tests/zz.py': 'x\n'}, False)]:
            with self.subTest(label):
                hub, api = self.scenario(update=update)
                if allowed:
                    self.assertIsNone(self.merge(hub, api)['verification_failure'])
                else:
                    self.refused(hub, api)

    def test_forbidden_file_in_the_validated_head_refuses_whatever_listings_say(self):
        # An ABA head (clean -> forbidden -> clean listing) cannot hide a file: the exact head's objects decide.
        hub, api = self.scenario(update={**UPDATE, 'scripts/evil.py': 'x\n'})
        api.pr['changed_files'] = 2
        self.refused(hub, api)
        # Intermediate commits are history; only the final trees are inspected, as the README says.
        hub, api = self.scenario(history=[{'scripts/check.py': 'check = 0\n'}, {'scripts/check.py': 'check = 1\n'}])
        self.assertIsNone(self.merge(hub, api)['verification_failure'])

    def test_ineligible_shapes_owner_and_official_exclusions_never_merge(self):
        shapes = [
            ('new entry', {'entries/new.json': {'name': 'new', 'owner': 'author'}, 'skills/new/x.py': '1\n'}),
            ('entry removed', {'entries/demo.json': None}),
            ('no entry', {'skills/demo/plugin.py': 'v2\n'}),
            ('second entry', {**UPDATE, 'entries/other.json': {'name': 'other', 'owner': 'author'}}),
            ('catalog', {**UPDATE, 'catalog.json': '{"x": 1}\n'}),
            ('official file', {**UPDATE, 'official.json': {'endorsements': [{'name': 'x'}]}}),
            ('workflow', {**UPDATE, owner.WORKFLOW: 'name: other\n'}),
            ('different payload', {**UPDATE, 'skills/other/a.py': 'a2\n'}),
            ('owner transfer', {**UPDATE, 'entries/demo.json': {'name': 'demo', 'owner': 'another'}}),
            ('rename in json', {**UPDATE, 'entries/demo.json': {'name': 'other', 'owner': 'author'}}),
            ('cross-skill move', {'entries/demo.json': UPDATE['entries/demo.json'], 'skills/other/a.py': None,
                                  'skills/demo/a.py': 'a1\n'}),
            ('symlink', {**UPDATE, 'skills/demo/link': ('symlink', '../../scripts/check.py')}),
        ]
        for label, update in shapes:
            with self.subTest(label):
                hub, api = self.scenario(update=update)
                self.refused(hub, api)
        for label, base in [('absent owner', {'entries/demo.json': {'name': 'demo'}}),
                            ('endorsed', {'official.json': {'endorsements': [{'name': 'demo', 'digest': 'any'}]}})]:
            with self.subTest(label):
                hub, api = self.scenario(base=base, update={**UPDATE, 'entries/demo.json': {
                    **(base.get('entries/demo.json') or UPDATE['entries/demo.json']), 'description': 'new'}})
                self.refused(hub, api)
        hub, api = self.scenario()
        for label, mutate in [('different author', lambda pr: pr['user'].update(login='intruder')),
                              ('draft', lambda pr: pr.update(draft=True)),
                              ('closed', lambda pr: pr.update(state='closed')),
                              ('wrong target', lambda pr: pr['base'].update(ref='signals')),
                              ('wrong repo', lambda pr: pr['base']['repo'].update(full_name='other/repo'))]:
            with self.subTest(label):
                api = API(hub)
                mutate(api.pr)
                self.refused(hub, api)
        api = API(hub)
        api.pr['user']['login'] = 'Author'
        self.assertIsNone(self.merge(hub, api)['verification_failure'])  # Logins compare case-insensitively.

    def test_payload_deletion_and_internal_rename_are_permitted(self):
        base = {'skills/demo/old.py': 'old\n', 'skills/demo/data.txt': 'data\n'}
        update = {**UPDATE, 'skills/demo/old.py': None, 'skills/demo/data.txt': None, 'skills/demo/sub/data.txt': 'data\n'}
        hub, api = self.scenario(base=base, update=update)
        self.assertEqual(sorted((f['status'], f['filename']) for f in owner.diff_files(hub.work, hub.base, hub.head)), [
            ('added', 'skills/demo/sub/data.txt'), ('modified', 'entries/demo.json'), ('modified', 'skills/demo/plugin.py'),
            ('removed', 'skills/demo/data.txt'), ('removed', 'skills/demo/old.py')])
        self.assertIsNone(self.merge(hub, api)['verification_failure'])
        self.assertEqual(git(hub.remote, 'ls-tree', '-r', '--name-only', 'refs/heads/main', '--', 'skills/demo').split(),
                         ['skills/demo/plugin.py', 'skills/demo/sub/data.txt'])
        # A removal outside the skill, even paired with an addition inside it, is not an internal rename.
        hub, api = self.scenario(update={**UPDATE, 'skills/other/a.py': None, 'skills/demo/a.py': 'a1\n'})
        self.refused(hub, api)

    def test_executable_payload_files_are_ordinary_files(self):
        hub, api = self.scenario(update={**UPDATE, 'skills/demo/run.sh': ('exec', '#!/bin/sh\n')})
        self.assertIsNone(self.merge(hub, api)['verification_failure'])

    def test_submodule_in_payload_is_refused(self):
        hub, api = self.scenario(update={**UPDATE, 'skills/demo/vendor': ('gitlink', 'c' * 40)})
        self.refused(hub, api)

    def test_main_may_change_other_skills_but_not_this_one(self):
        cases = [('unrelated skill', {'skills/other/a.py': 'a2\n'}, None),
                 ('generated catalog', {'catalog.json': '{"generated": true}\n'}, None),
                 ('same payload', {'skills/demo/plugin.py': 'v1b\n'}, 'update the branch'),
                 ('rename away on main', {'skills/demo/plugin.py': None, 'skills/other/plugin.py': 'v1\n'},
                  'update the branch'),
                 ('owner on main', {'entries/demo.json': {'name': 'demo', 'owner': 'author', 'note': 'x'}},
                  'update the branch'),
                 ('endorsed on main', {'official.json': {'endorsements': [{'name': 'demo'}]}}, 'Official'),
                 ('CI control on main', {'scripts/check.py': 'check = 2\n'}, 'CI control')]
        for label, change, refusal in cases:
            with self.subTest(label):
                hub, api = self.scenario()
                hub.advance_main(change)
                if refusal is None:
                    outcome = self.merge(hub, api)
                    self.assertIsNone(outcome['verification_failure'])
                else:
                    with self.assertRaisesRegex(owner.Ineligible, refusal):
                        self.merge(hub, api)
                    self.assertEqual(api.writes(), [])

    def test_tested_commit_must_merge_this_head_with_this_skill_into_main(self):
        hub, api = self.scenario()
        altered = hub.test_merge({'skills/demo/plugin.py': 'tampered\n'})
        for label, tested in [('head itself', hub.head), ('main itself', hub.base), ('altered skill', altered)]:
            with self.subTest(label):
                api = API(hub)
                api.receipt.update(tested_sha=tested, workflow_sha=tested)
                self.refused(hub, api)
        hub, api = self.scenario()  # First parent is not a main commit.
        git(hub.work, 'checkout', '-q', '--detach', hub.base)
        hub.commit({'scripts/check.py': 'check = 9\n'}, 'not main')
        git(hub.work, 'merge', '-q', '--no-ff', '-m', 'test merge', hub.head)
        side = git(hub.work, 'rev-parse', 'HEAD')
        hub.push(side, 'refs/tested/side')
        git(hub.work, 'checkout', '-q', 'update')
        api.receipt.update(tested_sha=side, workflow_sha=side)
        self.refused(hub, api)
        # A rerun reuses the run's tested merge, so the advice is a new validation, not a rerun.
        with self.assertRaisesRegex(owner.Ineligible, 'a rerun repeats the same tested merge'):
            self.merge(hub, api)

    def test_ci_success_is_bound_to_one_attempt_and_its_receipt(self):
        hub, _api = self.scenario()
        mutations = [
            ('failed run', lambda a: a.run.update(conclusion='failure')),
            ('push run', lambda a: a.run.update(event='push')),
            ('running', lambda a: a.run.update(status='in_progress')),
            ('other workflow', lambda a: a.run.update(path='other.yml')),
            ('newer attempt', lambda a: a.run.update(run_attempt=2)),
            ('checks failed', lambda a: a.jobs[0].update(conclusion='failure')),
            ('receipt job missing', lambda a: a.jobs.remove(next(j for j in a.jobs if j['name'] == 'receipt'))),
            ('catalog missing: checks and receipt rerun alone',
             lambda a: a.jobs.remove(next(j for j in a.jobs if j['name'] == 'catalog'))),
            ('catalog listed from an earlier attempt',
             lambda a: next(j for j in a.jobs if j['name'] == 'catalog').update(run_attempt=0)),
            ('catalog listed twice, once failed',
             lambda a: a.jobs.append({'name': 'catalog', 'status': 'completed', 'conclusion': 'failure'})),
            ('no receipt', lambda a: a.artifacts.clear()),
            ('other attempt receipt', lambda a: a.artifacts[0].update(name=f'{owner.RECEIPT}-90-2')),
            ('collision', lambda a: a.artifacts.append(copy.deepcopy(a.artifacts[0]))),
            ('expired', lambda a: a.artifacts[0].update(expired=True)),
            ('oversized', lambda a: a.artifacts[0].update(size_in_bytes=100000)),
            ('receipt attempt', lambda a: a.receipt.update(run_attempt=2)),
            ('receipt run', lambda a: a.receipt.update(run_id=89)),
            ('receipt repository', lambda a: a.receipt.update(repository='other/repo')),
            ('receipt head', lambda a: a.receipt.update(head_sha='c' * 40)),
            ('workflow sha', lambda a: a.receipt.update(workflow_sha=a.hub.head)),
            ('workflow path', lambda a: a.receipt.update(workflow_ref=f'{REPO}/.github/workflows/x.yml@refs/pull/12/merge')),
            ('pr moved', lambda a: a.pr['head'].update(sha='c' * 40)),
            ('head repository', lambda a: a.pr['head']['repo'].update(full_name='other/repo')),
            ('associated PR', lambda a: a.run.update(pull_requests=[{'number': 13}])),
        ]
        for label, mutate in mutations:
            with self.subTest(label):
                api = API(hub)
                mutate(api)
                self.refused(hub, api)
        api = API(hub)
        api.run.update(pull_requests=[], path=owner.WORKFLOW + '@refs/pull/12/merge')  # Fork runs list no PR.
        self.assertIsNone(self.merge(hub, api)['verification_failure'])
        # Jobs carrying this attempt's number pass; the field is optional and its absence alone never blocks.
        hub, api = self.scenario()
        for job in api.jobs:
            job['run_attempt'] = 1
        self.assertIsNone(self.merge(hub, api)['verification_failure'])

    def test_attempt_must_run_every_hosted_suite_the_change_selects(self):
        def job(name, **extra):
            return {'name': name, 'status': 'completed', 'conclusion': 'success', **extra}
        files = [{'filename': 'entries/smart_lists.json'}, {'filename': 'skills/smart_lists/w.py'}]
        # The selector is ci_checks' own: smart_lists belongs to two suites; an entry-only change selects none.
        self.assertEqual(owner.expected_jobs(files), owner.REQUIRED_JOBS | {'smart-lists', 'widgets'})
        self.assertEqual(owner.expected_jobs(files[:1]), owner.REQUIRED_JOBS)
        base = {'entries/smart_lists.json': {'name': 'smart_lists', 'owner': 'author'},
                'skills/smart_lists/w.py': 'v1\n'}
        update = {'entries/smart_lists.json': {'name': 'smart_lists', 'owner': 'author', 'description': 'new'},
                  'skills/smart_lists/w.py': 'v2\n'}
        hub, _api = self.scenario(base=base, update=update)
        cases = [('both selected suites', [job('smart-lists'), job('widgets')], None),
                 ('only the skipped placeholder', [], 'smart-lists, widgets'),
                 ('one suite missing', [job('smart-lists')], 'widgets'),
                 ('one suite failed', [job('smart-lists'), job('widgets', conclusion='failure')], 'widgets'),
                 ('one suite skipped', [job('smart-lists'), job('widgets', conclusion='skipped')], 'widgets'),
                 ('one suite inherited from an earlier attempt', [job('smart-lists'), job('widgets', run_attempt=0)],
                  'widgets')]
        for label, hosted, missing in cases:
            with self.subTest(label):
                api = API(hub)
                api.jobs.extend(hosted)
                if missing is None:
                    self.assertIsNone(self.merge(hub, api)['verification_failure'])
                    hub, _api = self.scenario(base=base, update=update)  # The merge moved main; start afresh.
                else:
                    with self.assertRaisesRegex(owner.Ineligible, missing):
                        self.merge(hub, api)
                    self.assertEqual(api.writes(), [])

    def test_the_last_read_before_the_request_rechecks_the_validation_attempt(self):
        for label, change in [('rerun started', {'run_attempt': 2, 'status': 'in_progress', 'conclusion': None}),
                              ('newer attempt finished', {'run_attempt': 2}),
                              ('run no longer successful', {'conclusion': 'failure'})]:
            with self.subTest(label):
                hub, api = self.scenario()
                api.on_run_read = lambda n, run: n == 2 and run.update(change)
                with self.assertRaises(owner.Ineligible):
                    self.merge(hub, api)
                self.assertEqual(api.puts, [])
                self.assertEqual(self.control(api)['phase'], 'not_applied')

    def test_receipt_archive_is_bounded_json_never_extracted_or_executed(self):
        hub, api = self.scenario()
        for filename in ('../receipt.json', 'script.py', '/receipt.json'):
            with self.assertRaises(owner.Ineligible):
                owner.read_receipt(archive(api.receipt, filename))
        for changes in ({'pr_number': True}, {'run_id': -1}, {'schema_version': True}, {'head_sha': 'main'},
                        {'extra': 'field'}, {'tested_sha': None}, {'workflow_ref': 5}):
            with self.assertRaises(owner.Ineligible):
                owner.read_receipt(archive({**api.receipt, **changes}))
        with self.assertRaises(owner.Ineligible):
            owner.read_receipt(archive({**api.receipt, 'workflow_ref': 'x' * 17000}))

    def test_strict_raw_diff_parser(self):
        zero, blob = '0' * 40, 'a' * 40
        record = lambda meta, path=b'skills/demo/x': meta.encode() + b'\0' + path + b'\0'
        self.assertEqual(owner.parse_raw_diff(b''), [])
        self.assertEqual(owner.parse_raw_diff(record(f':000000 100644 {zero} {blob} A')),
                         [{'status': 'added', 'filename': 'skills/demo/x'}])
        malformed = [record(f':100644 100644 {blob} {blob} M')[:-1], b':100644\0',
                     record(f':100644 100644 {blob} {blob} M') + b'extra',
                     record(f':100644 100644 {blob[:7]} {blob} M'), record(f':100644 100644 {blob} {blob} R100'),
                     record(f':100644 100644 {blob} {blob} A'), record(f':100644 000000 {blob} {zero} M'),
                     record(f':000000 100644 {zero} {blob} D')]
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                owner.parse_raw_diff(raw)
        for raw in (record(f':100644 100644 {blob} {blob} X'), record(f':100644 100644 {blob} {blob} U'),
                    record(f':000000 120000 {zero} {blob} A'), record(f':000000 160000 {zero} {blob} A'),
                    record(f':000000 040000 {zero} {blob} A'), record(f':160000 000000 {blob} {zero} D'),
                    record(f':000000 100644 {zero} {blob} A', b'skills/demo/\xff')):
            with self.subTest(raw=raw), self.assertRaises(owner.Ineligible):
                owner.parse_raw_diff(raw)


class NoticeHoldAndOutcomeTests(Case):
    def test_no_merge_request_without_a_confirmed_notice(self):
        for error in (APIError(422), APIError(502), RuntimeError('GitHub API transport failed'), TimeoutError(),
                      http.client.IncompleteRead(b'')):
            with self.subTest(error=type(error).__name__):
                hub, api = self.scenario()
                api.comment_error = error
                with self.assertRaises(owner.NotApplied):
                    self.merge(hub, api)
                self.assertEqual(api.puts, [])

    def test_each_attempt_writes_only_its_own_comment_and_impersonations_are_ignored(self):
        hub, api = self.scenario()
        api.comments = [bot_comment(owner.MARKER + '\n<!-- owner-update-control: {"phase":"attempting"} -->',
                                    40, login='intruder', kind='User'),
                        bot_comment(owner.render(REPO, {'phase': 'not_applied', 'pr': 12, 'head': 'd' * 40,
                                                        'owner_run': [1, 1], 'detail': 'main changed'}), 41),
                        bot_comment(owner.render(REPO, {'phase': 'held', 'pr': 12, 'head': 'd' * 40,
                                                        'owner_run': [2, 1]}), 42)]
        earlier = copy.deepcopy(api.comments)
        self.merge(hub, api)
        self.assertEqual(api.comments[:3], earlier)
        self.assertEqual(self.phases(api), ['attempting', 'not_applied', 'held', 'merged'])
        self.assertEqual(owner.control_of(api.comments[3]['body'])['head'], hub.head)
        self.assertEqual([c[1] for c in api.calls if c[0] == 'PATCH'], [f'/repos/{REPO}/issues/comments/503'])
        # An unreadable comment of the bot itself holds conservatively.
        hub, api = self.scenario()
        api.comments = [bot_comment(owner.MARKER + '\nedited by hand', 41)]
        with self.assertRaisesRegex(owner.Unknown, 'comment 41'):
            self.merge(hub, api)
        with self.assertRaisesRegex(owner.Unknown, 'hold not confirmed.*comment 41'):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual((api.puts, len(api.comments)), ([], 1))

    def test_ordinary_retries_add_one_record_per_attempt(self):
        # The first attempt is definitely refused by its final reads (a rerun started); the rerun merges.
        hub, api = self.scenario()
        api.on_run_read = lambda n, run: n == 2 and run.update(run_attempt=2)
        with self.assertRaisesRegex(owner.Ineligible, 'newer validation attempt'):
            self.merge(hub, api)
        first = copy.deepcopy(api.comments)
        api.on_run_read = None
        api.artifacts[0]['name'], api.receipt['run_attempt'] = f'{owner.RECEIPT}-90-2', 2
        self.assertIsNone(self.merge(hub, api, attempt=2, run=701)['verification_failure'])
        self.assertEqual(api.comments[:1], first)
        self.assertEqual(self.phases(api), ['not_applied', 'merged'])
        self.assertEqual([owner.control_of(c['body'])['owner_run'] for c in api.comments], [[700, 1], [701, 1]])

    def test_hold_label_stops_merging_on_entry_and_during_final_reads(self):
        hub, api = self.scenario()
        api.pr['labels'] = [{'name': owner.HOLD_LABEL}]  # Ready (not draft) while labelled: still held.
        self.refused(hub, api, owner.Held)
        hub, api = self.scenario()
        api.on_pr_read = lambda n, pr: n == 2 and pr['labels'].append({'name': owner.HOLD_LABEL})
        self.refused(hub, api, owner.Held, writes=True)
        self.assertEqual(self.control(api)['phase'], 'not_applied')
        hub, api = self.scenario()
        api.on_pr_read = lambda n, pr: n == 2 and hub.advance_main({'skills/other/a.py': 'a3\n'})
        with self.assertRaisesRegex(owner.Ineligible, 'main changed'):
            self.merge(hub, api)
        self.assertEqual((api.puts, self.control(api)['phase']), ([], 'not_applied'))

    def test_stale_notice_after_a_refused_final_check_is_reported_not_hidden(self):
        hub, api = self.scenario()
        def label_then_fail(n, pr):
            if n == 2:
                pr['labels'].append({'name': owner.HOLD_LABEL})
                api.comment_error = APIError(502)
        api.on_pr_read = label_then_fail
        with self.assertRaisesRegex(owner.NotApplied, 'sent no merge request'):
            self.merge(hub, api)
        self.assertEqual(api.puts, [])
        self.assertEqual(self.control(api)['phase'], 'attempting')

    def test_hold_is_confirmed_only_with_label_open_pr_and_no_attempt_in_flight(self):
        hub, api = self.scenario()
        self.assertEqual(owner.hold(api, REPO, 12, (800, 1)), hub.head)
        self.assertEqual(api.count('POST', '/labels'), 1)
        control = self.control(api)
        self.assertEqual((control['phase'], control['owner_run']), ('held', [800, 1]))
        self.assertIn('Hold run', api.comments[0]['body'])
        # A label set by the maintainer is a request; the hold run confirms it without re-adding it.
        hub, api = self.scenario()
        api.pr['labels'] = [{'name': owner.HOLD_LABEL}]
        owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual(api.count('POST', '/labels'), 0)
        self.assertEqual(self.control(api)['phase'], 'held')
        for label, mutate, error in [('merged first', lambda a: a.pr.update(merged=True, merge_commit_sha='f' * 40),
                                      owner.NotApplied),
                                     ('closed', lambda a: a.pr.update(state='closed'), owner.NotApplied),
                                     ('notice write lost', lambda a: setattr(a, 'comment_error', RuntimeError('x')),
                                      owner.Unknown)]:
            with self.subTest(label):
                hub, api = self.scenario()
                mutate(api)
                with self.assertRaises(error):
                    owner.hold(api, REPO, 12, (800, 1))

    def test_lost_hold_confirmation_reply_does_not_deny_the_recorded_effect(self):
        hub, api = self.scenario()
        api.comment_fate = 'lost'  # The API stores the comment, then loses its reply.
        with self.assertRaisesRegex(owner.Unknown, 'write is unconfirmed.*may appear later'):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual(self.phases(api), ['held'])
        self.assertTrue(owner.held(api.pr))
        self.assertEqual(api.puts, [])

    def test_release_is_explicit_label_removal(self):
        hub, api = self.scenario()
        owner.hold(api, REPO, 12, (800, 1))
        self.refused(hub, api, owner.Held, writes=True)  # Still labelled: a new validation cannot merge.
        comments = copy.deepcopy(api.comments)
        self.assertEqual(owner.control_of(comments[0]['body'])['phase'], 'held')
        api = API(hub)  # The maintainer removed the label; the hold comment remains.
        api.comments = copy.deepcopy(comments)  # A copy: the snapshot must not grow with the new attempt's comment.
        self.assertIsNone(self.merge(hub, api)['verification_failure'])
        self.assertEqual(api.comments[:1], comments)
        self.assertEqual(self.phases(api), ['held', 'merged'])

    def attempting(self, hub, run=600, phase='attempting', extra='', comment_id=500):
        record = {'phase': 'attempting', 'pr': 12, 'head': hub.head, 'validation_run': [80, 1], 'owner_run': [run, 1]}
        body = owner.render(REPO, record)
        if phase != 'attempting':
            body = body.replace('"phase":"attempting"', f'"phase":"{phase}"')
        return bot_comment(body + extra, comment_id)

    ACK = '\nMaintainer acknowledgement (2026-11-17): run 600 read back, no merge seen'

    def acknowledge(self, api, comment_id=500):
        """The documented maintainer edit of one attempt's comment."""
        row = next(c for c in api.comments if c['id'] == comment_id)
        row['body'] = row['body'].replace('"phase":"attempting"', '"phase":"acknowledged_unknown"') + self.ACK

    def test_acknowledged_unknown_never_confirms_a_hold_and_a_later_definite_attempt_does_not_hide_it(self):
        # UNKNOWN -> acknowledgement -> Hold: the label stays, the hold is not confirmed, the comment is untouched.
        hub, api = self.scenario()
        api.comments = [self.attempting(hub, phase='acknowledged_unknown', extra=self.ACK)]
        before = copy.deepcopy(api.comments)
        with self.assertRaisesRegex(owner.Unknown, 'hold not confirmed.*600'):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual(api.comments, before)
        self.assertTrue(owner.held(api.pr))
        # UNKNOWN -> acknowledgement -> a new attempt GitHub definitely refuses -> Hold: the earlier attempt keeps
        # its own record, which still blocks the confirmation.
        hub, api = self.scenario()
        api.comments = [self.attempting(hub, phase='acknowledged_unknown', extra=self.ACK)]
        before = copy.deepcopy(api.comments)
        api.put_error = APIError(409)
        with self.assertRaises(owner.NotApplied):
            self.merge(hub, api)
        self.assertEqual((api.comments[:1], self.phases(api)), (before, ['acknowledged_unknown', 'not_applied']))
        with self.assertRaisesRegex(owner.Unknown, 'hold not confirmed.*600'):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual(self.phases(api), ['acknowledged_unknown', 'not_applied'])
        # A merge observed later settles it, keeping the maintainer's acknowledgement; the refused one stays.
        api.pr.update(merged=True, state='closed', merge_commit_sha='f' * 40)
        with self.assertRaises(owner.NotApplied):
            owner.hold(api, REPO, 12, (800, 1))
        control = owner.control_of(api.comments[0]['body'])
        self.assertEqual((control['phase'], control['observed_only'], control['owner_run']), ('merged', True, [600, 1]))
        self.assertIn(self.ACK.strip(), api.comments[0]['body'])
        self.assertEqual(self.phases(api), ['merged', 'not_applied'])
        # Same through the merge entry path, with a final-read refusal as the later definite attempt.
        hub, api = self.scenario()
        api.comments = [self.attempting(hub, phase='acknowledged_unknown', extra=self.ACK)]
        api.on_pr_read = lambda n, pr: n == 2 and pr['labels'].append({'name': owner.HOLD_LABEL})
        with self.assertRaises(owner.Held):
            self.merge(hub, api)
        with self.assertRaisesRegex(owner.Unknown, 'hold not confirmed'):
            owner.hold(api, REPO, 12, (800, 1))
        # A record of the earlier single-comment form that carried an acknowledged unknown is read conservatively.
        hub, api = self.scenario()
        api.comments = [bot_comment(owner.render(REPO, {'phase': 'not_applied', 'pr': 12, 'head': hub.head,
                                                        'owner_run': [601, 1], 'detail': 'x',
                                                        'prior': [{'owner_run': [600, 1]}]}), 500)]
        self.assertIsNone(self.merge(hub, api)['verification_failure'])
        hub, api = self.scenario()
        api.comments = [bot_comment(owner.render(REPO, {'phase': 'not_applied', 'pr': 12, 'head': hub.head,
                                                        'owner_run': [601, 1], 'detail': 'x',
                                                        'prior': [{'owner_run': [600, 1]}]}), 500)]
        with self.assertRaisesRegex(owner.Unknown, 'hold not confirmed.*601'):
            owner.hold(api, REPO, 12, (800, 1))

    def test_unresolved_attempt_blocks_merge_until_acknowledged_and_hold_entirely(self):
        hub, api = self.scenario()
        api.comments = [self.attempting(hub)]
        before = copy.deepcopy(api.comments)
        with self.assertRaisesRegex(owner.Unknown, '600'):
            self.merge(hub, api)
        with self.assertRaisesRegex(owner.Unknown, '600'):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual((api.puts, api.comments), ([], before))
        # Changing the phase without a dated reason is not an acknowledgement; neither is a stale run disappearing.
        api = API(hub)
        api.comments = [self.attempting(hub, phase='acknowledged_unknown')]
        with self.assertRaises(owner.Unknown):
            self.merge(hub, api)
        # Each unknown attempt needs its own acknowledgement.
        api = API(hub)
        api.comments = [self.attempting(hub, phase='acknowledged_unknown', extra=self.ACK),
                        self.attempting(hub, run=601, comment_id=501)]
        with self.assertRaisesRegex(owner.Unknown, 'comment 501'):
            self.merge(hub, api)
        api = API(hub)
        api.comments = [self.attempting(hub, phase='acknowledged_unknown', extra=self.ACK)]
        before = copy.deepcopy(api.comments)
        self.assertIsNone(self.merge(hub, api)['verification_failure'])
        self.assertEqual((api.comments[:1], self.phases(api)), (before, ['acknowledged_unknown', 'merged']))

    def test_a_late_write_of_an_earlier_attempt_cannot_erase_a_newer_unknown(self):
        # A: its notice is confirmed, a final read refuses (a rerun started), and its not_applied note times out
        # while GitHub may still apply it.
        hub, api = self.scenario()
        def time_out_notes(n, run):
            if n == 2:
                run.update(run_attempt=2)
                api.comment_fate = 'late'
        api.on_run_read = time_out_notes
        with self.assertRaisesRegex(owner.NotApplied, 'run 700 sent no merge request'):
            self.merge(hub, api)
        self.assertEqual((self.phases(api), len(api.late)), (['attempting'], 1))
        with self.assertRaisesRegex(owner.Unknown, 'comment 500'):
            owner.hold(api, REPO, 12, (800, 1))
        api.pr['labels'] = []  # The maintainer releases the hold that was not confirmed.
        # The maintainer reads run 700 back and acknowledges it; B, on the rerun, sends its request: UNKNOWN.
        self.acknowledge(api)
        api.on_run_read, api.comment_fate = None, None
        api.artifacts[0]['name'], api.receipt['run_attempt'] = f'{owner.RECEIPT}-90-2', 2
        api.put_error = APIError(502)
        with self.assertRaisesRegex(owner.Unknown, 'outcome is unknown'):
            self.merge(hub, api, attempt=2, run=701)
        self.assertEqual(self.phases(api), ['acknowledged_unknown', 'attempting'])
        # A's late write lands now. It replaces only A's own comment, with what is true of A.
        api.late.pop()()
        self.assertEqual(self.phases(api), ['not_applied', 'attempting'])
        self.assertEqual(owner.control_of(api.comments[1]['body'])['outcome'], 'unknown')
        before = copy.deepcopy(api.comments)
        with self.assertRaisesRegex(owner.Unknown, 'comment 501'):
            self.merge(hub, api, attempt=2, run=702)
        with self.assertRaisesRegex(owner.Unknown, 'hold not confirmed.*comment 501.*701'):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual((api.comments, len(api.puts)), (before, 1))
        # Acknowledging B authorizes a new merge attempt, never the hold; an observed merge settles B.
        self.acknowledge(api, 501)
        with self.assertRaisesRegex(owner.Unknown, 'hold not confirmed.*comment 501'):
            owner.hold(api, REPO, 12, (800, 1))
        api.pr.update(merged=True, state='closed', merge_commit_sha='f' * 40)
        with self.assertRaisesRegex(owner.NotApplied, 'merged before the hold'):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertEqual(self.phases(api), ['not_applied', 'merged'])

    def test_a_notice_whose_response_was_lost_holds_honestly(self):
        # GitHub created the comment but the response was lost: no request is sent, and the comment that appears
        # holds every later run until a maintainer reads this run back.
        hub, api = self.scenario()
        api.comment_fate = 'lost'
        with self.assertRaisesRegex(owner.NotApplied, 'no merge request was sent'):
            self.merge(hub, api)
        self.assertEqual((api.puts, self.phases(api)), ([], ['attempting']))
        api.comment_fate = None
        with self.assertRaisesRegex(owner.Unknown, 'comment 500.*700'):
            self.merge(hub, api, run=701)
        self.acknowledge(api)
        self.assertIsNone(self.merge(hub, api, run=702)['verification_failure'])
        self.assertEqual(self.phases(api), ['acknowledged_unknown', 'merged'])

    def test_positively_observed_merge_closes_an_unresolved_attempt(self):
        hub, api = self.scenario()
        api.comments = [self.attempting(hub)]
        api.pr.update(merged=True, state='closed', merge_commit_sha='f' * 40)
        with self.assertRaisesRegex(owner.Ineligible, 'already merged'):
            self.merge(hub, api)
        self.assertEqual((self.control(api)['phase'], self.control(api)['merge_commit']), ('merged', 'f' * 40))
        self.assertEqual(api.puts, [])
        # Observed, not verified: no claim that the merge kept the contribution's commits.
        body = api.comments[0]['body']
        self.assertIn('not verified by Owner Updates', body)
        self.assertNotIn("keeps the contribution's commits", body)
        self.assertTrue(self.control(api)['observed_only'])
        # The same truthful closure when a hold finds the PR already merged.
        hub, api = self.scenario()
        api.comments = [self.attempting(hub)]
        api.pr.update(merged=True, state='closed', merge_commit_sha='f' * 40)
        with self.assertRaises(owner.NotApplied):
            owner.hold(api, REPO, 12, (800, 1))
        self.assertIn('not verified by Owner Updates', api.comments[0]['body'])
        self.assertNotIn("keeps the contribution's commits", api.comments[0]['body'])

    def test_merge_request_outcomes_are_definite_or_unknown_and_never_resubmitted(self):
        for error, expected, phase in [(APIError(409), owner.NotApplied, 'not_applied'),
                                       (APIError(405), owner.NotApplied, 'not_applied'),
                                       (APIError(502), owner.Unknown, 'attempting'),
                                       (APIError(408), owner.Unknown, 'attempting'),
                                       (RuntimeError('GitHub API transport failed'), owner.Unknown, 'attempting'),
                                       (TimeoutError(), owner.Unknown, 'attempting'),
                                       # The request was sent and its response was cut off.
                                       (http.client.IncompleteRead(b'{"merged"'), owner.Unknown, 'attempting'),
                                       (http.client.RemoteDisconnected('closed'), owner.Unknown, 'attempting')]:
            with self.subTest(error=repr(error)):
                hub, api = self.scenario()
                api.put_error = error
                with self.assertRaises(expected):
                    self.merge(hub, api)
                self.assertEqual(len(api.puts), 1)
                self.assertEqual(self.control(api)['phase'], phase)
                if phase == 'attempting':
                    self.assertEqual(self.control(api)['outcome'], 'unknown')
        hub, api = self.scenario()
        api.put_result = {'merged': False}
        with self.assertRaises(owner.NotApplied):
            self.merge(hub, api)

    def test_post_merge_audit_reads_the_actual_parents_and_keeps_the_merged_outcome(self):
        hub, api = self.scenario()
        api.before_put = lambda: hub.advance_main({'skills/other/a.py': 'a9\n'})
        outcome = self.merge(hub, api)
        self.assertIsNone(outcome['verification_failure'])
        self.assertNotEqual(git(hub.remote, 'rev-parse', f"{outcome['merge_commit']}^1"), hub.base)
        for label, change in [('same skill', {'skills/demo/extra.txt': 'x\n'}),
                              ('endorsed', {'official.json': {'endorsements': [{'name': 'demo'}]}})]:
            with self.subTest(label):
                hub, api = self.scenario()
                api.before_put = lambda: hub.advance_main(change)
                outcome = self.merge(hub, api)
                self.assertTrue(outcome['verification_failure'])
                self.assertTrue(outcome['commits_kept'])  # Parents were read back; the policy check failed.
                self.assertEqual(self.control(api)['phase'], 'merged')
                self.assertIn('Post-merge verification failed', api.comments[0]['body'])
                self.assertIn("keeps the contribution's commits", api.comments[0]['body'])
        # Merged stays the fact; without a successful parents readback no kept commits are claimed.
        for label, sha, failure in [('unreadable merge commit', 'e' * 40, 'the merge commit could not be read back'),
                                    ('head is not a parent', None,
                                     'the merge commit does not keep the validated head as its second parent')]:
            with self.subTest(label):
                hub, api = self.scenario()
                api.put_result = {'merged': True, 'sha': sha or hub.base}  # hub.base: one parent, not the head.
                outcome = self.merge(hub, api)
                self.assertEqual((outcome['verification_failure'], outcome['commits_kept']), (failure, False))
                control = self.control(api)
                self.assertEqual((control['phase'], control['commits_kept']), ('merged', False))
                body = api.comments[0]['body']
                self.assertIn('Merged by merge commit', body)
                self.assertNotIn("keeps the contribution's commits", body)
                self.assertIn("did not confirm the contribution's head among its parents", body)
        hub, api = self.scenario()
        api.before_put = lambda: setattr(api, 'comment_error', APIError(502))  # Final note lost: still merged.
        outcome = self.merge(hub, api)
        self.assertEqual((outcome['verification_failure'], outcome['notice_updated']), (None, False))
        self.assertTrue(api.pr['merged'])
        self.assertEqual(self.control(api)['phase'], 'attempting')  # What the public comment still says.
        # No later merge run comes for a merged PR; an explicit Hold dispatch records the merge as observed.
        api.comment_error = None
        with self.assertRaisesRegex(owner.NotApplied, 'merged before the hold'):
            owner.hold(api, REPO, 12, (800, 1))
        control = self.control(api)
        self.assertEqual((control['phase'], control['observed_only'], control['merge_commit']),
                         ('merged', True, outcome['merge_commit']))
        self.assertIn('not verified by Owner Updates', api.comments[0]['body'])
        self.assertEqual(len(api.puts), 1)


class CommandLineTests(Case):
    def run_cli(self, hub, api, argv, **env):
        summary = Path(self.tmp.name) / f'summary{self.count}.md'
        values = {'GITHUB_REPOSITORY': REPO, 'GITHUB_RUN_ID': '700', 'GITHUB_RUN_ATTEMPT': '1',
                  'GITHUB_STEP_SUMMARY': str(summary), **env}
        with patch.dict(os.environ, values), patch.object(owner, 'GitHub', return_value=api), \
                patch.object(owner, 'ROOT', hub.trusted):
            code = owner.main(argv)
        return code, summary.read_text(encoding='utf-8') if summary.exists() else ''

    def test_product_cli_merges_and_reports_in_the_step_summary(self):
        hub, api = self.scenario()
        event = Path(self.tmp.name) / 'event.json'
        event.write_text(json.dumps({'workflow_run': {'id': 90, 'run_attempt': 1}}), encoding='utf-8')
        code, summary = self.run_cli(hub, api, ['merge', '--event', str(event)])
        self.assertEqual(code, 0)
        self.assertIn('Merged owner update #12', summary)
        self.assertIn('"owner_run":[700,1]', api.comments[0]['body'])
        hub, api = self.scenario()
        api.put_error = APIError(502)
        code, summary = self.run_cli(hub, api, ['merge', '--event', str(event)])
        self.assertEqual(code, 1)
        self.assertIn('UNKNOWN', summary)
        # A response cut off after the request was sent: UNKNOWN in the summary, not a bare traceback.
        hub, api = self.scenario()
        api.put_error = http.client.IncompleteRead(b'')
        code, summary = self.run_cli(hub, api, ['merge', '--event', str(event)])
        self.assertEqual((code, len(api.puts), self.control(api)['outcome']), (1, 1, 'unknown'))
        self.assertIn('UNKNOWN: the merge request outcome is unknown', summary)
        # Merged, but the final note was not confirmed: refused (the comment still shows the attempt) or applied with
        # its response lost (the comment already says merged). The summary claims neither and the run is not green.
        for label, before_put, phase in [
                ('refused', lambda a: setattr(a, 'comment_error', APIError(502)), 'attempting'),
                ('response lost', lambda a: setattr(a, 'comment_fate', 'lost'), 'merged')]:
            with self.subTest(label):
                hub, api = self.scenario()
                api.before_put = lambda: before_put(api)
                code, summary = self.run_cli(hub, api, ['merge', '--event', str(event)])
                self.assertEqual((code, self.control(api)['phase']), (1, phase))
                self.assertTrue(api.pr['merged'])
                self.assertIn('Merged owner update #12', summary)
                self.assertIn('update of the public notice was not confirmed', summary)
                self.assertIn('may still show the attempt', summary)
                self.assertNotIn('could not be updated', summary)
                self.assertIn('Hold run dispatched for this PR records the merge as observed', summary)
        # Merged without a parents readback: the summary keeps the merge and says what was not confirmed.
        hub, api = self.scenario()
        api.put_result = {'merged': True, 'sha': 'e' * 40}
        code, summary = self.run_cli(hub, api, ['merge', '--event', str(event)])
        self.assertEqual(code, 1)
        self.assertIn('Merged owner update #12', summary)
        self.assertIn("head was not confirmed among the merge commit's parents", summary)

    def test_privileged_helper_imports_only_the_standard_library(self):
        # Owner Updates installs nothing: its selector import from ci_checks must not pull in PyYAML.
        code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); sys.modules["yaml"] = None; '
                'import scripts.owner_updates as o; assert "smart-lists" in o.selected_suites({"skills/smart_lists/x"})')
        subprocess.run([sys.executable, '-I', '-B', '-c', code], check=True, capture_output=True, timeout=60)

    def test_product_cli_hold_runs_only_from_main_for_one_positive_number(self):
        hub, api = self.scenario()
        for argv, ref in [(['hold', '--pr', '12'], 'refs/heads/feature'), (['hold', '--pr', '0'], 'refs/heads/main'),
                          (['hold', '--pr', '12abc'], 'refs/heads/main'), (['hold'], 'refs/heads/main')]:
            with self.subTest(argv=argv, ref=ref):
                code, _ = self.run_cli(hub, api, argv, GITHUB_REF=ref)
                self.assertEqual((code, api.calls), (1, []))
        code, summary = self.run_cli(hub, api, ['hold', '--pr', '12'], GITHUB_REF='refs/heads/main')
        self.assertEqual(code, 0)
        self.assertIn('Hold confirmed for PR #12', summary)


class ReceiptWorkflowTests(Case):
    def test_receipt_job_script_records_what_owner_updates_accepts(self):
        workflow = yaml.load((ROOT / '.github/workflows/catalog-check.yml').read_text(encoding='utf-8'),
                             Loader=yaml.BaseLoader)
        step = workflow['jobs']['receipt']['steps'][0]
        temp = Path(self.tmp.name) / 'runner'
        temp.mkdir()
        tested, head = 'b' * 40, 'a' * 40
        subprocess.run(['/bin/sh', '-c', step['run']], check=True, capture_output=True, timeout=60, env={
            **os.environ, 'RUNNER_TEMP': str(temp), 'GITHUB_REPOSITORY': REPO, 'GITHUB_RUN_ID': '90',
            'GITHUB_RUN_ATTEMPT': '2', 'PR_NUMBER': '12', 'HEAD_SHA': head, 'TESTED_SHA': tested,
            'WORKFLOW_REF': f'{REPO}/{owner.WORKFLOW}@refs/pull/12/merge', 'WORKFLOW_SHA': tested})
        data = (temp / 'owner-receipt/receipt.json').read_bytes()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as bundle:
            bundle.writestr('receipt.json', data)
        receipt = owner.read_receipt(stream.getvalue())
        self.assertEqual((receipt['run_attempt'], receipt['tested_sha'], receipt['head_sha']), (2, tested, head))
