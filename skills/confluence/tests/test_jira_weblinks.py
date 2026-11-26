"""Offline Jira web-link removal: the real helper, the printed argv and a stateful fake TWG."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from confluence_skill_under_test import plugin

PAYLOAD = Path(__file__).resolve().parents[1]
HELPER = PAYLOAD / 'jira_weblinks.py'
FAKE = Path(__file__).with_name('fake_twg.py')
MR = 'https://git.example.invalid/demo/app/-/merge_requests/7'
OTHER = 'https://docs.example.invalid/demo/design'


def remote(identifier, url, title):
    return {'id': identifier, 'self': f'https://example.invalid/rest/api/3/issue/10042/remotelink/{identifier}',
            'globalId': f'system={url}', 'application': {'type': 'com.example.git', 'name': 'Example Git'},
            'relationship': 'links to', 'object': {'url': url, 'title': title}}


@pytest.fixture
def jira(tmp_path):
    """A synthetic DEMO-42: the MR URL in the description text and in two remote links, plus an unrelated link."""
    state = {
        'site': 'example', 'profile': 'demo-reader',
        'issue': {'id': '10042', 'key': 'DEMO-42', 'summary': 'Synthetic task',
                  'description': f'Merged in {MR}', 'issuelinks': [{'id': '20001', 'outwardIssue': 'DEMO-7'}]},
        'mr': {'url': MR, 'state': 'merged'},
        'links': [remote(10001, MR, '!7 first'), remote(10002, MR, '!7 second'), remote(10003, OTHER, 'Design')],
        'faults': {'get': [], 'unlink': []}, 'calls': [], 'deletes': [], 'other_id': 10003,
    }
    path = tmp_path / 'fake-state.json'
    path.write_text(json.dumps(state), encoding='utf-8')

    class Jira:
        state_path = path
        workdir = tmp_path / 'task-drive' / 'demo-42-weblink'
        emitted = []

        def read(self):
            return json.loads(path.read_text(encoding='utf-8'))

        def fault(self, command, *modes):
            value = self.read()
            value['faults'][command] = list(modes)
            path.write_text(json.dumps(value), encoding='utf-8')

        def helper(self, *args):
            # cwd mirrors run_command(cwd=skill_payload); nothing is written beside the helper.
            done = subprocess.run([sys.executable, '-I', str(HELPER), *args], cwd=PAYLOAD,
                                  capture_output=True, text=True, timeout=60)
            out = json.loads(done.stdout)
            for step in ([out['run_command']] if isinstance(out.get('run_command'), dict)
                         else out.get('run_command') or []):
                self.emitted.append(step['cmd'])
            return done.returncode, out

        def twg(self, cmd):
            # Explicit fixture runner: the printed program is `twg` itself; only the test maps it to the fake.
            assert cmd[0] == 'twg' and all(isinstance(part, str) for part in cmd)
            done = subprocess.run([sys.executable, '-I', str(FAKE), *cmd[1:]], capture_output=True, text=True,
                                  timeout=60, env={**os.environ, 'FAKE_TWG_STATE': str(path)})
            assert 'Traceback' not in done.stderr, done.stderr
            return done.returncode

        def plan(self, url=MR):
            args = ['plan', '--workdir', str(self.workdir), '--site', 'example', '--issue', 'demo-42',
                    '--profile', 'demo-reader'] + (['--url', url] if url else [])
            return self.helper(*args)

        def prepared(self, link_id='10002', url=MR, select_url=None):
            code, out = self.plan(url)
            assert code == 0
            self.twg(out['run_command']['cmd'])
            return self.helper('select', '--workdir', str(self.workdir), '--link-id', link_id,
                               *(['--url', select_url] if select_url else []))

        def run_steps(self, chosen):
            return [self.twg(step['cmd']) for step in chosen['run_command']]

        def verify(self):
            return self.helper('verify', '--workdir', str(self.workdir))

    yield Jira()
    for cmd in Jira.emitted:  # every test: nothing emitted asks TWG for human (user) mode
        assert cmd[0] == 'twg' and cmd[cmd.index('--mode') + 1] == 'agent' and cmd.count('--mode') == 1
        assert 'user' not in cmd


def test_selected_duplicate_is_the_only_change_and_is_verified(jira):
    code, out = jira.plan()
    assert code == 0
    assert out['run_command']['cmd'] == [
        'twg', '--profile', 'demo-reader', '--mode', 'agent', '--output-summary', 'none',
        '--output-file', str(jira.workdir / 'before.json'), 'jira', 'workitem', 'get', 'DEMO-42',
        '--remote-links', '--fields', 'summary', '--site', 'example', '--output', 'json']
    assert stat.S_IMODE(jira.workdir.stat().st_mode) == 0o700
    assert stat.S_IMODE((jira.workdir / 'plan.json').stat().st_mode) == 0o600
    assert jira.twg(out['run_command']['cmd']) == 0

    code, listing = jira.helper('list', '--workdir', str(jira.workdir))
    assert code == 0
    assert [(link['id'], link['matches_expected_url']) for link in listing['web_links']] == [
        ('10001', True), ('10002', True), ('10003', False)]
    assert listing['issue'] == {'id': '10042', 'key': 'DEMO-42', 'url': 'https://example.invalid/browse/DEMO-42',
                                'site': 'example'}
    assert 'Merged in' not in json.dumps(listing)

    code, chosen = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10002')
    assert code == 0 and chosen['other_links_with_same_url'] == ['10001']
    delete, after = (step['cmd'] for step in chosen['run_command'])
    assert delete == ['twg', '--profile', 'demo-reader', '--mode', 'agent', '--output-summary', 'none',
                      '--output-file', str(jira.workdir / 'unlink.json'), 'jira', 'workitem', 'unlink', 'weblink',
                      '--id', '10042', '--link-id', '10002', '--site', 'example', '--output', 'json']
    assert '--url' not in delete
    assert jira.twg(delete) == 0 and jira.twg(after) == 0

    code, report = jira.verify()
    assert code == 0 and report['outcome'] == 'removed' and report['write'] == {'status': 'acknowledged'}
    assert report['other_links'] == {'missing': [], 'changed': []} and report['target_present'] is False

    state = jira.read()
    assert state['deletes'] == [['10042', '10002']]
    assert [link['id'] for link in state['links']] == [10001, 10003]
    assert state['issue']['description'] == f'Merged in {MR}'
    assert state['issue']['issuelinks'] == [{'id': '20001', 'outwardIssue': 'DEMO-7'}]
    assert state['mr'] == {'url': MR, 'state': 'merged'}
    assert [call[:3] for call in state['calls']] == [['jira', 'workitem', 'get'], ['jira', 'workitem', 'unlink'],
                                                     ['jira', 'workitem', 'get']]
    assert sorted(path.name for path in PAYLOAD.iterdir() if path.suffix == '.json') == []


def test_listing_before_the_url_is_known_then_selecting_with_it(jira):
    code, out = jira.plan(url=None)
    assert code == 0 and '--url' in out['next']
    jira.twg(out['run_command']['cmd'])
    code, listing = jira.helper('list', '--workdir', str(jira.workdir))
    assert code == 0 and listing['expected_url'] is None
    assert [link['url'] for link in listing['web_links']] == [MR, MR, OTHER]
    assert all('matches_expected_url' not in link for link in listing['web_links'])
    code, refused = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10001')
    assert code == 2 and refused['error'] == 'url_required'
    code, chosen = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10001', '--url', MR)
    assert code == 0 and chosen['selected']['id'] == '10001'
    jira.run_steps(chosen)
    code, report = jira.verify()
    assert code == 0 and report['outcome'] == 'removed'
    assert [link['id'] for link in jira.read()['links']] == [10002, 10003]


def test_select_url_cannot_differ_from_the_planned_url(jira):
    code, out = jira.prepared('10003', select_url=OTHER)
    assert code == 2 and out['error'] == 'url_mismatch'
    assert jira.read()['deletes'] == []


def test_no_replay_after_a_selection_or_a_delete_attempt(jira):
    code, chosen = jira.prepared()
    assert code == 0
    code, again = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10001')
    assert code == 2 and again['error'] == 'already_attempted'
    jira.twg(chosen['run_command'][0]['cmd'])
    code, again = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10002')
    assert code == 2 and again['error'] == 'already_attempted'
    code, replan = jira.plan()
    assert code == 2 and replan['error'] == 'workdir_not_fresh'
    assert len(jira.read()['deletes']) == 1


@pytest.mark.parametrize('link_id,error', [('10003', 'url_mismatch'), ('10009', 'link_not_found'),
                                           ('not-a-number', 'invalid_argument')])
def test_selection_needs_the_exact_id_whose_url_matches(jira, link_id, error):
    code, out = jira.prepared(link_id)
    assert code == 2 and out['error'] == error
    assert not (jira.workdir / 'selection.json').exists()
    assert jira.read()['deletes'] == []


@pytest.mark.parametrize('fault,error', [
    ('omit_remote_links', 'remote_links_missing'), ('truncated', 'malformed_json'), ('no_file', 'missing_file'),
    ('provider_error', 'provider_error'), ('non_list', 'malformed_remote_links'), ('partial', 'incomplete_read'),
    ('duplicate_ids', 'duplicate_link_id'), ('wrong_key', 'context_mismatch'),
])
def test_unusable_before_read_selects_nothing(jira, fault, error):
    jira.fault('get', fault)
    code, out = jira.prepared()
    assert code == 2 and out['error'] == error
    assert jira.read()['deletes'] == [] and not (jira.workdir / 'selection.json').exists()


def test_lost_acknowledgement_reports_absence_without_claiming_the_delete(jira):
    jira.fault('unlink', 'lost_ack')
    _, chosen = jira.prepared()
    assert jira.run_steps(chosen) == [1, 0]
    code, report = jira.verify()
    assert code == 3 and report['outcome'] == 'absent_without_acknowledgement'
    assert report['write'] == {'status': 'no_acknowledgement', 'reason': 'missing_file',
                               'detail': 'delete was not written: unlink.json is absent.'}
    assert len(jira.read()['deletes']) == 1


def test_provider_refusal_differs_from_a_missing_acknowledgement(jira):
    jira.fault('unlink', 'refuse')
    _, chosen = jira.prepared()
    assert jira.run_steps(chosen) == [1, 0]
    code, report = jira.verify()
    assert code == 3 and report['outcome'] == 'refused' and report['target_present'] is True
    assert report['write']['status'] == 'provider_error' and report['write']['error']['statusCode'] == 403
    assert jira.read()['deletes'] == []


def test_write_without_any_output_is_not_removed(jira):
    jira.fault('unlink', 'crash')
    _, chosen = jira.prepared()
    jira.run_steps(chosen)
    code, report = jira.verify()
    assert code == 3 and report['outcome'] == 'not_removed' and report['write']['status'] == 'no_acknowledgement'


@pytest.mark.parametrize('fault', ['no_file', 'omit_remote_links', 'truncated', 'provider_error'])
def test_failed_follow_up_read_is_unverified(jira, fault):
    _, chosen = jira.prepared()
    jira.fault('get', fault)
    jira.run_steps(chosen)
    code, report = jira.verify()
    assert code == 3 and report['outcome'] == 'unverified' and report['write']['status'] == 'acknowledged'


@pytest.mark.parametrize('fault,field', [('drop_other', 'missing'), ('retitle_other', 'changed')])
def test_any_change_to_another_link_record_is_never_a_clean_removal(jira, fault, field):
    _, chosen = jira.prepared()
    jira.fault('get', fault)
    jira.run_steps(chosen)
    code, report = jira.verify()
    assert code == 3 and report['outcome'] == 'other_links_changed'
    assert report['other_links'][field] == ['10003'] and report['target_present'] is False


def test_changed_identity_or_edited_before_read_stops_verification(jira):
    _, chosen = jira.prepared()
    jira.fault('get', 'other_issue')
    jira.run_steps(chosen)
    code, report = jira.verify()
    assert code == 3 and report['outcome'] == 'identity_changed' and report['after_issue']['id'] == '10999'
    before = jira.workdir / 'before.json'
    before.write_text(before.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    code, report = jira.verify()
    assert code == 2 and report['error'] == 'before_changed'


@pytest.mark.parametrize('change,error', [
    ({'workdir': 'relative/dir'}, 'invalid_argument'), ({'workdir': str(PAYLOAD / 'run')}, 'invalid_argument'),
    ({'issue': 'DEMO-42 OR 1'}, 'invalid_argument'), ({'url': 'javascript:alert(1)'}, 'invalid_argument'),
    ({'profile': 'two words'}, 'invalid_argument'), ({'site': 'example.invalid/x?'}, 'invalid_argument'),
])
def test_plan_refuses_unbound_or_unsafe_context(jira, change, error):
    args = {'workdir': str(jira.workdir), 'site': 'example', 'profile': 'demo-reader', 'issue': 'DEMO-42',
            'url': MR, **change}
    code, out = jira.helper('plan', *[part for key, value in args.items() for part in (f'--{key}', value)])
    assert code == 2 and out['error'] == error
    assert not (PAYLOAD / 'run').exists()


def test_profile_is_required_and_no_custom_executable_is_accepted(jira):
    for extra in ([], ['--profile', 'demo-reader', '--twg', '/tmp/other']):
        done = subprocess.run([sys.executable, '-I', str(HELPER), 'plan', '--workdir', str(jira.workdir),
                               '--site', 'example', '--issue', 'DEMO-42', *extra],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 2 and done.stdout == '' and not jira.workdir.exists()


def test_helper_is_a_pure_standard_library_tool_and_wiki_tools_are_unchanged():
    tree = ast.parse(HELPER.read_text(encoding='utf-8'))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {'__future__', 'argparse', 'hashlib', 'json', 'os', 're', 'pathlib', 'urllib.parse'}
    called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
              and node.func.value.id == 'os'}
    assert called == {'open', 'fdopen'}
    assert len(plugin.TOOLS) == 12 and not any('jira' in name for name in plugin.TOOLS)
