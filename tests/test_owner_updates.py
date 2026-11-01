"""Both directions of the owner-update authorization boundary, with a fake GitHub API."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from scripts.github_api import APIError
from scripts import owner_updates as owner

REPO = 'razzant/OuroborosHub'
HEAD, BASE = 'a' * 40, 'b' * 40


def archive(value, filename='verdict.json'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        bundle.writestr(filename, json.dumps(value))
    return stream.getvalue()


class API:
    def __init__(self):
        self.calls = []
        self.pr = {'number': 12, 'state': 'open', 'draft': False, 'merged': False, 'changed_files': 2,
                   'user': {'login': 'author'}, 'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': REPO}},
                   'head': {'sha': HEAD, 'repo': {'full_name': 'author/OuroborosHub'}}}
        self.run = {'event': 'pull_request', 'status': 'completed', 'conclusion': 'success', 'path': owner.WORKFLOW,
                    'repository': {'full_name': REPO}, 'head_repository': {'full_name': 'author/OuroborosHub'},
                    'head_sha': HEAD, 'run_attempt': 1, 'pull_requests': [{'number': 12}]}
        self.jobs = [{'name': n, 'status': 'completed', 'conclusion': 'success'} for n in owner.REQUIRED_JOBS]
        self.files = [{'filename': 'entries/demo.json', 'status': 'modified'},
                      {'filename': 'skills/demo/plugin.py', 'status': 'modified'}]
        self.base_entry = {'name': 'demo', 'owner': 'author'}
        self.head_entry = {**self.base_entry, 'description': 'new'}
        self.official = {'endorsements': []}
        self.verdict = {'schema_version': 1, 'repository': REPO, 'pr_number': 12, 'head_sha': HEAD,
                        'base_sha': BASE, 'run_id': 90, 'run_attempt': 1, 'eligible': True, 'name': 'demo'}
        self.artifacts = [{'id': 100, 'name': owner.ARTIFACT, 'expired': False, 'size_in_bytes': 500}]
        self.latest = None
        self.latest_main = None
        self.archive = None
        self.merge_result = {'merged': True}

    def request(self, method, path, body=None, *, raw=False):
        self.calls.append((method, path, body))
        if method == 'PUT':
            return self.merge_result
        if path.endswith('/actions/runs/90'):
            return copy.deepcopy(self.run)
        if path.endswith('/pulls/12'):
            reads = sum(c[1].endswith('/pulls/12') for c in self.calls)
            return copy.deepcopy(self.latest if reads > 1 and self.latest is not None else self.pr)
        if path.endswith('/git/ref/heads/main'):
            reads = sum(c[1].endswith('/git/ref/heads/main') for c in self.calls)
            return {'object': {'sha': self.latest_main if reads > 1 and self.latest_main else BASE}}
        if path.endswith('/actions/artifacts/100/zip'):
            assert raw
            return self.archive or archive(self.verdict)
        raise AssertionError((method, path))

    def pages(self, path, key=None):
        self.calls.append(('pages', path, None))
        if path.endswith('/jobs'):
            return copy.deepcopy(self.jobs)
        if path.endswith('/artifacts'):
            return copy.deepcopy(self.artifacts)
        if path.endswith('/files'):
            return copy.deepcopy(self.files)
        raise AssertionError(path)

    def contents(self, repository, path, ref):
        self.calls.append(('contents', path, ref))
        assert repository == REPO
        if path == 'official.json':
            assert ref == BASE
            return copy.deepcopy(self.official)
        assert path == 'entries/demo.json'
        row = self.base_entry if ref == BASE else self.head_entry
        if row is None:
            raise APIError(404)
        return copy.deepcopy(row)


class OwnerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.api = API()

    def merge(self):
        return owner.merge_owner_update(self.api, {'workflow_run': {'id': 90}}, REPO)

    def test_existing_community_owner_merges_exact_validated_head(self):
        self.assertEqual(owner.REQUIRED_JOBS, {'checks'})
        self.assertEqual(self.merge(), 12)
        self.assertEqual([c for c in self.api.calls if c[0] == 'PUT'], [
            ('PUT', f'/repos/{REPO}/pulls/12/merge', {'sha': HEAD, 'merge_method': 'squash'})])
        self.assertEqual({c[1] for c in self.api.calls if c[0] == 'contents'}, {'entries/demo.json', 'official.json'})

    def test_aggregate_success_allows_unaffected_suites_to_be_skipped(self):
        self.api.jobs.append({'name': 'hosted-skills', 'status': 'completed', 'conclusion': 'skipped'})
        self.assertEqual(self.merge(), 12)
        for conclusion in ('failure', 'cancelled', 'skipped'):
            self.api = API()
            self.api.jobs[0]['conclusion'] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaises(owner.Ineligible):
                self.merge()
            self.assertFalse(any(c[0] == 'PUT' for c in self.api.calls))

    def test_fork_run_without_associated_pr_list_still_binds_head_repository_and_sha(self):
        self.api.run['pull_requests'] = []
        self.api.run['path'] = owner.WORKFLOW + '@refs/pull/12/merge'
        self.assertEqual(self.merge(), 12)
        self.api = API()
        self.api.pr['user']['login'] = 'Author'
        self.assertEqual(self.merge(), 12)  # GitHub login comparison is case insensitive.

    def test_ineligible_update_shapes_never_merge(self):
        changes = [
            ('new entry', lambda a: a.files[0].update(status='added')),
            ('entry removed', lambda a: a.files[0].update(status='removed')),
            ('no entry', lambda a: a.files.pop(0)),
            ('legacy publisher', lambda a: a.files[0].update(filename='catalog.json')),
            ('second entry', lambda a: a.files.append({'filename': 'entries/other.json', 'status': 'modified'})),
            ('catalog', lambda a: a.files.append({'filename': 'catalog.json', 'status': 'modified'})),
            ('official file', lambda a: a.files.append({'filename': 'official.json', 'status': 'modified'})),
            ('workflow', lambda a: a.files.append({'filename': owner.WORKFLOW, 'status': 'modified'})),
            ('different payload', lambda a: a.files[1].update(filename='skills/other/plugin.py')),
            ('path traversal', lambda a: a.files[1].update(filename='skills/demo/../../scripts/x.py')),
            ('cross-skill rename', lambda a: a.files[1].update(status='renamed', previous_filename='skills/other/x.py')),
            ('different owner', lambda a: a.pr['user'].update(login='intruder')),
            ('owner transfer', lambda a: a.head_entry.update(owner='another')),
            ('absent owner', lambda a: a.base_entry.pop('owner')),
            ('missing on main', lambda a: setattr(a, 'base_entry', None)),
            ('rename in json', lambda a: a.head_entry.update(name='other')),
            ('endorsed', lambda a: a.official['endorsements'].append({'name': 'demo', 'digest': 'any'})),
            ('draft', lambda a: a.pr.update(draft=True)),
            ('closed', lambda a: a.pr.update(state='closed')),
            ('merged', lambda a: a.pr.update(merged=True)),
            ('wrong target', lambda a: a.pr['base'].update(ref='signals')),
            ('wrong repo', lambda a: a.pr['base']['repo'].update(full_name='other/repo')),
        ]
        for label, mutate in changes:
            with self.subTest(label=label):
                self.api = API()
                mutate(self.api)
                self.api.pr['changed_files'] = len(self.api.files)
                with self.assertRaises(owner.Ineligible):
                    self.merge()
                self.assertFalse(any(c[0] == 'PUT' for c in self.api.calls))

    def test_payload_add_remove_and_internal_rename_are_permitted(self):
        for status, extra in [('added', {}), ('removed', {}), ('renamed', {'previous_filename': 'skills/demo/old.py'})]:
            with self.subTest(status=status):
                self.api = API()
                self.api.files[1].update(status=status, **extra)
                self.assertEqual(self.merge(), 12)

    def test_artifact_and_success_are_bound_to_run_attempt_pr_and_head(self):
        mutations = [
            lambda a: a.run.update(conclusion='failure'), lambda a: a.run.update(event='push'),
            lambda a: a.run.update(status='in_progress'), lambda a: a.run.update(path='other.yml'),
            lambda a: a.run.update(path=owner.WORKFLOW + '.other@main'),
            lambda a: a.jobs[0].update(conclusion='skipped'), lambda a: a.jobs.pop(),
            lambda a: a.artifacts.clear(), lambda a: a.artifacts.append(copy.deepcopy(a.artifacts[0])),
            lambda a: a.artifacts[0].update(expired=True), lambda a: a.artifacts[0].update(size_in_bytes=100000),
            lambda a: a.verdict.update(run_id=89), lambda a: a.verdict.update(run_attempt=2),
            lambda a: a.verdict.update(repository='other/repo'), lambda a: a.verdict.update(eligible=False),
            lambda a: a.verdict.update(name='other'), lambda a: a.verdict.update(head_sha='c' * 40),
            lambda a: a.pr['head'].update(sha='c' * 40),
            lambda a: a.pr['head']['repo'].update(full_name='other/repo'),
            lambda a: a.run.update(pull_requests=[{'number': 13}]), lambda a: a.pr.update(changed_files=3),
        ]
        for mutate in mutations:
            self.api = API()
            mutate(self.api)
            with self.assertRaises(owner.Ineligible):
                self.merge()
            self.assertFalse(any(c[0] == 'PUT' for c in self.api.calls))

    def test_races_are_rechecked_immediately_before_exact_sha_merge(self):
        for scenario in ('head', 'author', 'main', 'closed'):
            with self.subTest(scenario=scenario):
                self.api = API()
                self.api.latest = copy.deepcopy(self.api.pr)
                if scenario == 'head':
                    self.api.latest['head']['sha'] = 'c' * 40
                elif scenario == 'author':
                    self.api.latest['user']['login'] = 'intruder'
                elif scenario == 'closed':
                    self.api.latest['state'] = 'closed'
                else:
                    self.api.latest_main = 'd' * 40
                with self.assertRaises(owner.Ineligible):
                    self.merge()
                self.assertFalse(any(c[0] == 'PUT' for c in self.api.calls))
        self.api = API()
        self.api.merge_result = {'merged': False}
        with self.assertRaisesRegex(RuntimeError, 'declined'):
            self.merge()

    def test_archive_is_only_bounded_json_never_extracted_or_executed(self):
        for filename in ('../verdict.json', 'script.py', '/verdict.json'):
            with self.assertRaises(owner.Ineligible):
                owner.read_verdict(archive(self.api.verdict, filename))
        for changes in ({'pr_number': True}, {'run_id': -1}, {'schema_version': True}, {'head_sha': 'main'},
                        {'extra': 'field'}, {'eligible': 1}):
            with self.assertRaises(owner.Ineligible):
                owner.read_verdict(archive({**self.api.verdict, **changes}))
        huge = {**self.api.verdict, 'name': 'x' * 17000}
        with self.assertRaises(owner.Ineligible):
            owner.read_verdict(archive(huge))

    def test_read_only_verdict_from_real_local_git_history(self):
        with tempfile.TemporaryDirectory(prefix='hub-owner-verdict-') as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                                                *args], cwd=root, stderr=subprocess.DEVNULL, timeout=30).decode().strip()
            git('init', '-q', '-b', 'main')
            (root / 'entries').mkdir()
            (root / 'entries/demo.json').write_text(json.dumps(self.api.base_entry), encoding='utf-8')
            (root / 'official.json').write_text(json.dumps(self.api.official), encoding='utf-8')
            git('add', '.')
            git('commit', '-qm', 'admit')
            base = git('rev-parse', 'HEAD')
            git('checkout', '-qb', 'update')
            (root / 'entries/demo.json').write_text(json.dumps(self.api.head_entry), encoding='utf-8')
            git('commit', '-qam', 'update')
            head = git('rev-parse', 'HEAD')
            pr = copy.deepcopy(self.api.pr)
            pr['head']['sha'], pr['base']['sha'] = head, base
            event = {'pull_request': pr, 'repository': {'full_name': REPO}}
            verdict = owner.make_verdict(root, event, 90, 1)
            self.assertTrue(verdict['eligible'])
            self.assertEqual((verdict['name'], verdict['head_sha']), ('demo', head))
            pr['user']['login'] = 'intruder'
            self.assertFalse(owner.make_verdict(root, event, 90, 1)['eligible'])
            self.assertEqual(git('status', '--porcelain'), '')
