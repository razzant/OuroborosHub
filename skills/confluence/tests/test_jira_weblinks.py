"""Offline Jira web-link helper and fake-TWG consumer regressions."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

PAYLOAD = Path(__file__).resolve().parents[1]
HELPER = PAYLOAD / 'jira_weblinks.py'
FAKE = Path(__file__).with_name('fake_twg.py')
MR = 'https://git.example.invalid/demo/app/-/merge_requests/7'
OTHER = 'https://docs.example.invalid/demo/design'

# Clean host environment baseline for tests: strips any TWG_ and PYTHON* vars from the runner's shell.
BASE_ENV = {k: v for k, v in os.environ.items() if not k.upper().startswith(('TWG_', 'PYTHON'))}


def python_command(env=None, *flags):
    # -I ignores every PYTHON* variable, so a run that restricts the stream encoding uses -s -B instead.
    isolation = ['-s', '-B'] if 'PYTHONIOENCODING' in (env or {}) else ['-I']
    return [sys.executable, *isolation, *flags]


def package_inventory():
    """Every payload file with its content hash; interpreter bytecode caches are not payload."""
    return {path.relative_to(PAYLOAD).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in PAYLOAD.rglob('*') if path.is_file() and '__pycache__' not in path.parts}


def twg_error(code, status=None, kind=None):
    """A failed-command error in the shape of TWG's public output reference, with synthetic text."""
    body = {'code': code, 'kind': kind or code.lower(), 'message': f'Synthetic {code} failure',
            'repair': 'twg login --force', 'retry': {'recommended': False, 'guidance': 'do_not_retry_unchanged'}}
    if status is not None:
        body['statusCode'] = status
    return body


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

        def write_state(self, value):
            path.write_text(json.dumps(value), encoding='utf-8')

        def fault(self, command, *modes):
            value = self.read()
            value['faults'][command] = list(modes)
            self.write_state(value)

        def helper_raw(self, *args, env=None, isolated_stdlib=False):
            run_env = {**BASE_ENV, **(env or {})}
            command = python_command(env, *(['-S'] if isolated_stdlib else []))
            # cwd mirrors run_command(cwd=skill_payload); nothing is written beside the helper.
            return subprocess.run([*command, str(HELPER), *args], cwd=PAYLOAD,
                                  capture_output=True, text=True, timeout=60, env=run_env)

        def helper(self, *args, env=None, isolated_stdlib=False):
            done = self.helper_raw(*args, env=env, isolated_stdlib=isolated_stdlib)
            assert 'Traceback' not in done.stderr, done.stderr
            out = json.loads(done.stdout)
            for step in ([out['run_command']] if isinstance(out.get('run_command'), dict)
                         else out.get('run_command') or []):
                self.emitted.append(step['cmd'])
            return done.returncode, out

        def twg(self, cmd, env=None):
            # Explicit fixture runner: only the test maps bare `twg` to fake_twg.py.
            assert cmd[0] == 'twg' and all(isinstance(part, str) for part in cmd)
            run_env = {**BASE_ENV, **(env or {}), 'FAKE_TWG_STATE': str(path)}
            done = subprocess.run([*python_command(env), str(FAKE), *cmd[1:]], capture_output=True, text=True,
                                  timeout=60, env=run_env)
            assert 'Traceback' not in done.stderr, done.stderr
            return done.returncode

        def plan(self, url=MR, *, env=None):
            args = ['plan', '--workdir', str(self.workdir), '--site', 'example', '--issue', 'demo-42',
                    '--profile', 'demo-reader']
            if url is not None:
                args += ['--url', url]
            return self.helper(*args, env=env)

        def prepared(self, link_id='10002', url=MR, select_url=None, *, env=None):
            code, out = self.plan(url, env=env)
            assert code == 0
            self.twg(out['run_command']['cmd'], env=env)
            return self.helper('select', '--workdir', str(self.workdir), '--link-id', link_id,
                               *(['--url', select_url] if select_url is not None else []), env=env)

        def run_steps(self, chosen, env=None):
            return [self.twg(step['cmd'], env=env) for step in chosen['run_command']]

        def verify(self, env=None):
            return self.helper('verify', '--workdir', str(self.workdir), env=env)

    instance = Jira()
    shipped = package_inventory()
    yield instance
    for cmd in instance.emitted:  # every test: bare `twg` in agent mode, never human (user) mode
        assert cmd[0] == 'twg' and cmd[cmd.index('--mode') + 1] == 'agent' and cmd.count('--mode') == 1
        assert 'user' not in cmd
    assert package_inventory() == shipped  # no run adds, changes or removes anything in the skill package


def test_selected_duplicate_is_the_only_change_and_surviving_same_url_is_reported(jira):
    code, out = jira.plan()
    assert code == 0
    assert out['run_command']['cmd'] == [
        'twg', '--profile', 'demo-reader', '--mode', 'agent', '--output-summary', 'none',
        '--output-file', str(jira.workdir / 'before.json'), 'jira', 'workitem', 'get', 'DEMO-42',
        '--remote-links', '--fields', 'summary', '--site', 'example', '--output', 'json']
    if os.name == 'posix':
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
    assert 'atomic' in chosen['url_check_scope'].lower()
    delete, after = (step['cmd'] for step in chosen['run_command'])
    assert delete == ['twg', '--profile', 'demo-reader', '--mode', 'agent', '--output-summary', 'none',
                      '--output-file', str(jira.workdir / 'unlink.json'), 'jira', 'workitem', 'unlink', 'weblink',
                      '--id', '10042', '--link-id', '10002', '--site', 'example', '--output', 'json']
    assert '--url' not in delete
    assert jira.twg(delete) == 0 and jira.twg(after) == 0

    code, report = jira.verify()
    assert code == 0 and report['outcome'] == 'removed'
    assert report['write'] == {'status': 'acknowledged'}
    assert report['other_links'] == {'missing': [], 'changed': []} and report['target_present'] is False
    assert report['surviving_same_url_links'] == ['10001']
    assert report['new_links'] == []

    state = jira.read()
    assert state['deletes'] == [['10042', '10002']]
    assert [link['id'] for link in state['links']] == [10001, 10003]
    assert state['issue']['description'] == f'Merged in {MR}'
    assert state['issue']['issuelinks'] == [{'id': '20001', 'outwardIssue': 'DEMO-7'}]
    assert state['mr'] == {'url': MR, 'state': 'merged'}
    assert [call[:3] for call in state['calls']] == [['jira', 'workitem', 'get'], ['jira', 'workitem', 'unlink'],
                                                     ['jira', 'workitem', 'get']]


def test_removing_the_only_web_link_ends_with_an_explicit_empty_list(jira):
    state = jira.read()
    state['links'] = state['links'][:1]
    jira.write_state(state)
    code, chosen = jira.prepared('10001')
    assert code == 0 and chosen['other_links_with_same_url'] == []
    assert jira.run_steps(chosen) == [0, 0]
    after = json.loads((jira.workdir / 'after.json').read_text(encoding='utf-8'))
    assert after['data'][0]['remoteLinks'] == []
    code, report = jira.verify()
    assert code == 0 and report['outcome'] == 'removed' and report['target_present'] is False
    assert report['other_links'] == {'missing': [], 'changed': []}
    assert report['new_links'] == [] and report['surviving_same_url_links'] == []
    code, listing = jira.helper('list', '--workdir', str(jira.workdir), '--which', 'after')
    assert code == 0 and listing['web_links'] == []


def test_read_only_listing_of_an_issue_without_web_links_is_empty(jira):
    state = jira.read()
    state['links'] = []
    jira.write_state(state)
    _, out = jira.plan(url=None)
    assert jira.twg(out['run_command']['cmd']) == 0
    code, listing = jira.helper('list', '--workdir', str(jira.workdir))
    assert code == 0 and listing['web_links'] == []
    assert jira.read()['deletes'] == [] and not (jira.workdir / 'selection.json').exists()


def test_list_only_is_a_finished_read_only_outcome(jira):
    code, out = jira.plan(url=None)
    assert code == 0
    assert 'read-only' in out['next'].lower() and 'stop after `list`' in out['next'].lower()
    assert jira.twg(out['run_command']['cmd']) == 0
    code, listing = jira.helper('list', '--workdir', str(jira.workdir))
    assert code == 0 and listing['expected_url'] is None
    assert 'stop here' in listing['next'].lower()
    assert [link['url'] for link in listing['web_links']] == [MR, MR, OTHER]
    assert all('matches_expected_url' not in link for link in listing['web_links'])
    assert not (jira.workdir / 'selection.json').exists()
    assert jira.read()['deletes'] == []
    assert [call[:3] for call in jira.read()['calls']] == [['jira', 'workitem', 'get']]


def test_listing_before_the_url_is_known_then_selecting_with_it(jira):
    code, out = jira.plan(url=None)
    assert code == 0 and '--url' in out['next']
    jira.twg(out['run_command']['cmd'])
    code, refused = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10001')
    assert code == 2 and refused['error'] == 'url_required'
    code, chosen = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10001', '--url', MR)
    assert code == 0 and chosen['selected']['id'] == '10001'
    jira.run_steps(chosen)
    code, report = jira.verify()
    assert code == 0 and report['outcome'] == 'removed'
    assert report['surviving_same_url_links'] == ['10002']
    assert [link['id'] for link in jira.read()['links']] == [10002, 10003]


def test_new_same_url_sibling_is_reported_without_deleting_extra_records(jira):
    _, chosen = jira.prepared()
    jira.fault('get', 'add_same_url_sibling')
    assert jira.run_steps(chosen) == [0, 0]
    code, report = jira.verify()
    assert code == 0 and report['outcome'] == 'removed'
    assert report['new_links'] == ['10004']
    assert report['surviving_same_url_links'] == ['10001', '10004']
    assert jira.read()['deletes'] == [['10042', '10002']]


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
    ('warnings', 'incomplete_read'), ('duplicate_ids', 'duplicate_link_id'), ('wrong_key', 'context_mismatch'),
])
def test_unusable_before_read_selects_nothing(jira, fault, error):
    jira.fault('get', fault)
    code, out = jira.prepared()
    assert code == 2 and out['error'] == error
    assert jira.read()['deletes'] == [] and not (jira.workdir / 'selection.json').exists()


@pytest.mark.parametrize('fault,reason,expected_code,expected_status', [
    ('timeout_present', 'provider_error', 'timeout', None),
    ('http_408_present', 'provider_error', 'request_timeout', 408),
    ('http_504_present', 'provider_error', 'gateway_timeout', 504),
    ('http_500_present', 'provider_error', 'server_error', 500),
    ('unfamiliar_present', 'provider_error', 'unfamiliar_fault', None),
    ('crash', 'missing_file', None, None),
    ('mismatched_ack', 'mismatched_acknowledgement', None, None),
    # A status never shows which request failed: a transport failure can carry an earlier read's status.
    pytest.param(twg_error('TWG_NETWORK_ERROR', 403, 'network'), 'provider_error', 'TWG_NETWORK_ERROR', 403,
                 id='network_error_with_403'),
    pytest.param(twg_error('AUTH_INVALID', 401), 'provider_error', 'AUTH_INVALID', 401, id='credential_401'),
    pytest.param(twg_error('AUTH_INVALID', 403), 'provider_error', 'AUTH_INVALID', 403, id='credential_403'),
    pytest.param(twg_error('TWG_COMMAND_FAILED', 403), 'provider_error', 'TWG_COMMAND_FAILED', 403,
                 id='generic_failure_403'),
    pytest.param(twg_error('unfamiliar_code', 404), 'provider_error', 'unfamiliar_code', 404,
                 id='unfamiliar_code_404'),
])
def test_unacknowledged_delete_with_the_link_still_present_stays_ambiguous(
        jira, fault, reason, expected_code, expected_status):
    jira.fault('unlink', fault)
    _, chosen = jira.prepared()
    jira.run_steps(chosen)
    code, report = jira.verify()
    assert code == 3 and report['ok'] is False and report['outcome'] == 'ambiguous_present'
    assert report['target_present'] is True
    assert report['surviving_same_url_links'] == ['10001', '10002']
    assert report['write']['status'] == 'ambiguous'
    assert report['write']['reason'] == reason
    if expected_code is not None:
        assert report['write']['error']['code'] == expected_code
        assert report['write']['error']['statusCode'] == expected_status
        assert 'repair' not in report['write']['error']  # vendor repair hints are not relayed for execution
    assert 'do not resend' in report['note'].lower()
    assert jira.read()['deletes'] == []
    # Replay in the same workdir remains blocked.
    code, replay = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10002')
    assert code == 2 and replay['error'] == 'already_attempted'


@pytest.mark.parametrize('fault,reason', [
    ('timeout_absent', 'provider_error'),
    ('http_504_absent', 'provider_error'),
    ('lost_ack', 'missing_file'),
])
def test_ambiguous_delete_when_absent_does_not_attribute_cause(jira, fault, reason):
    jira.fault('unlink', fault)
    _, chosen = jira.prepared()
    assert jira.run_steps(chosen) == [1, 0]
    code, report = jira.verify()
    assert code == 3 and report['ok'] is False and report['outcome'] == 'absent_without_acknowledgement'
    assert report['target_present'] is False
    assert report['surviving_same_url_links'] == ['10001']
    assert report['write']['status'] == 'ambiguous'
    assert report['write']['reason'] == reason
    assert 'attribute' in report['note'].lower()
    assert len(jira.read()['deletes']) == 1


def test_acknowledged_but_still_present_is_non_success_and_never_replayed(jira):
    jira.fault('unlink', 'ack_still_present')
    _, chosen = jira.prepared()
    assert jira.run_steps(chosen) == [0, 0]
    code, report = jira.verify()
    assert code == 3 and report['ok'] is False and report['outcome'] == 'acknowledged_but_present'
    assert report['write']['status'] == 'acknowledged'
    assert report['target_present'] is True
    assert report['surviving_same_url_links'] == ['10001', '10002']
    code, replay = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10002')
    assert code == 2 and replay['error'] == 'already_attempted'


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


@pytest.mark.parametrize('bad_url', [
    'javascript:alert(1)',
    'https://[::1',
    'https://example.invalid:badport/x',
    'https://example.invalid:99999/x',
    'https://:80/path',
    'https://example.invalid/has space',
    '',
])
def test_malformed_url_syntax_returns_typed_json_never_traceback(jira, bad_url):
    code, out = jira.plan(url=bad_url)
    assert code == 2 and out['error'] == 'invalid_argument' and not jira.workdir.exists()
    code, planned = jira.plan(url=None)
    assert code == 0
    jira.twg(planned['run_command']['cmd'])
    code, sel = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10001', '--url', bad_url)
    assert code == 2 and sel['error'] == 'invalid_argument'


def test_non_ascii_titles_and_paths_emit_lossless_json_under_ascii_pipe_encoding(jira, tmp_path):
    env = {'PYTHONIOENCODING': 'ascii'}
    probe = subprocess.run([*python_command(env), '-c', 'import sys; print(sys.stdout.encoding)'],
                           capture_output=True, text=True, timeout=60, env={**BASE_ENV, **env})
    assert probe.stdout.strip() == 'ascii', 'the restriction must reach both the helper and the fake TWG'
    unicode_title = 'Задача — MR !7 ✓'
    state = jira.read()
    state['links'][0]['object']['title'] = unicode_title
    jira.write_state(state)
    jira.workdir = tmp_path / 'задача-папка' / 'run-1'
    code, planned = jira.plan(env=env)
    assert code == 0 and Path(planned['workdir']) == jira.workdir.resolve()
    assert jira.twg(planned['run_command']['cmd'], env=env) == 0
    raw = jira.helper_raw('list', '--workdir', str(jira.workdir), env=env)
    assert raw.returncode == 0 and raw.stderr == ''
    raw.stdout.encode('ascii')  # pure ASCII on piped stdout
    listing = json.loads(raw.stdout)
    assert listing['web_links'][0]['title'] == unicode_title
    code, chosen = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10002', env=env)
    assert code == 0 and jira.run_steps(chosen, env=env) == [0, 0]
    code, report = jira.verify(env=env)
    assert code == 0 and report['outcome'] == 'removed'


def test_existing_empty_writable_dir_and_symlink_workdir_are_refused(jira, tmp_path):
    empty = tmp_path / 'already-exists-empty'
    empty.mkdir()
    jira.workdir = empty
    code, out = jira.plan()
    assert code == 2 and out['error'] == 'workdir_not_fresh'
    assert list(empty.iterdir()) == []
    if os.name == 'posix':
        dangling = tmp_path / 'dangling-link'
        dangling.symlink_to(tmp_path / 'never-created')
        jira.workdir = dangling
        code, out = jira.plan()
        assert code == 2 and out['error'] == 'workdir_not_fresh'
        assert not (tmp_path / 'never-created').exists()


@pytest.mark.parametrize('change,error', [
    ({'workdir': 'relative/dir'}, 'invalid_argument'), ({'workdir': str(PAYLOAD / 'run')}, 'invalid_argument'),
    ({'issue': 'DEMO-42 OR 1'}, 'invalid_argument'),
    ({'profile': 'two words'}, 'invalid_argument'), ({'profile': 'demo.reader'}, 'invalid_argument'),
    ({'profile': 'reader@example'}, 'invalid_argument'), ({'profile': 'читатель'}, 'invalid_argument'),
    ({'profile': 'p' * 65}, 'invalid_argument'), ({'profile': ''}, 'invalid_argument'),
    ({'site': 'example.invalid/x?'}, 'invalid_argument'), ({'site': '_example'}, 'invalid_argument'),
])
def test_plan_refuses_unbound_or_unsafe_context(jira, change, error):
    args = {'workdir': str(jira.workdir), 'site': 'example', 'profile': 'demo-reader', 'issue': 'DEMO-42',
            'url': MR, **change}
    code, out = jira.helper('plan', *[part for key, value in args.items() for part in (f'--{key}', value)])
    assert code == 2 and out['error'] == error
    assert not (PAYLOAD / 'run').exists() and not jira.workdir.exists()


@pytest.mark.parametrize('profile,selector', [
    ('demo-reader', ['--profile', 'demo-reader']),
    ('default', ['--profile', 'default']),
    ('_personal', ['--profile', '_personal']),
    ('p' * 64, ['--profile', 'p' * 64]),
    ('-ops_2', ['--profile=-ops_2']),  # a separate argument starting with "-" would read as another option
])
def test_every_valid_profile_name_reaches_twg_as_one_unambiguous_selector(jira, profile, selector):
    state = jira.read()
    state['profile'] = profile
    jira.write_state(state)
    code, out = jira.helper('plan', '--workdir', str(jira.workdir), '--site', 'example', '--issue', 'DEMO-42',
                            f'--profile={profile}', '--url', MR)
    assert code == 0
    assert jira.twg(out['run_command']['cmd']) == 0
    code, chosen = jira.helper('select', '--workdir', str(jira.workdir), '--link-id', '10002')
    assert code == 0 and jira.run_steps(chosen) == [0, 0]
    for step in [out['run_command'], *chosen['run_command']]:
        assert step['cmd'][1:1 + len(selector)] == selector
        assert step['cmd'][1 + len(selector)] == '--mode'
    code, report = jira.verify()
    assert code == 0 and report['outcome'] == 'removed'


@pytest.mark.parametrize('var_name', ['TWG_TOKEN', 'TWG_OAUTH_ACCESS_TOKEN'])
def test_personal_jira_auth_overrides_are_declined_without_echoing_values(jira, var_name):
    sentinel = 'synthetic-secret-must-never-appear-7391'
    override_env = {var_name: sentinel}
    raw_plan = jira.helper_raw('plan', '--workdir', str(jira.workdir), '--site', 'example',
                               '--issue', 'DEMO-42', '--profile', 'demo-reader', '--url', MR, env=override_env)
    assert raw_plan.returncode == 2 and sentinel not in raw_plan.stdout and sentinel not in raw_plan.stderr
    out = json.loads(raw_plan.stdout)
    assert out['error'] == 'ambient_auth_override' and var_name in out['variables']
    assert not jira.workdir.exists()

    # Even if plan ran cleanly, select also declines when a personal-Jira override appears in the host env.
    code, planned = jira.plan()
    assert code == 0
    jira.twg(planned['run_command']['cmd'])
    raw_sel = jira.helper_raw('select', '--workdir', str(jira.workdir), '--link-id', '10002', env=override_env)
    assert raw_sel.returncode == 2 and sentinel not in raw_sel.stdout and sentinel not in raw_sel.stderr
    sel_out = json.loads(raw_sel.stdout)
    assert sel_out['error'] == 'ambient_auth_override' and var_name in sel_out['variables']
    assert not (jira.workdir / 'selection.json').exists()


def test_unrelated_and_storage_twg_env_vars_do_not_block_saved_profile_flow(jira):
    unrelated = {
        'TWG_AUTH_STORAGE_KEY': 'synthetic-key-placeholder',
        'TWG_ADMIN_TOKEN': 'synthetic-admin-only-placeholder',
        'TWG_USER': 'synthetic-user-without-token',
        'TWG_PROFILE': 'other',
        'TWG_AGENT_DEFAULTS': '1',
        'TWG_OAUTH_HINT': 'unrelated-prefix-must-not-be-wildcard-banned',
    }
    code, chosen = jira.prepared(env=unrelated)
    assert code == 0 and chosen['selected']['id'] == '10002'


def test_profile_is_required_and_no_executable_override_flag_exists(jira):
    done = jira.helper_raw('plan', '--workdir', str(jira.workdir), '--site', 'example', '--issue', 'DEMO-42')
    assert done.returncode == 2 and done.stdout == '' and not jira.workdir.exists()
    extra = jira.helper_raw('plan', '--workdir', str(jira.workdir), '--site', 'example',
                            '--issue', 'DEMO-42', '--profile', 'demo-reader', '--twg-bin', '/usr/local/bin/twg')
    assert extra.returncode == 2 and extra.stdout == '' and not jira.workdir.exists()


def test_helper_preserves_stdlib_only_and_no_network_or_process_boundary(jira):
    tree = ast.parse(HELPER.read_text(encoding='utf-8'))
    top_modules = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
    top_modules |= {node.module.split('.')[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module}
    assert top_modules <= sys.stdlib_module_names
    forbidden_modules = {'subprocess', 'socket', 'ssl', 'http', 'ftplib', 'smtplib', 'xmlrpc',
                         'asyncio', 'multiprocessing', 'pty', 'shutil', 'webbrowser', 'ctypes'}
    assert top_modules.isdisjoint(forbidden_modules)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('urllib.'):
            assert node.module == 'urllib.parse'
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'os':
            assert not node.attr.startswith(('system', 'popen', 'spawn', 'exec', 'fork', 'posix_spawn'))
    # Behavioral proof: runs with -I -S (no site-packages) cleanly.
    code, out = jira.helper('plan', '--workdir', str(jira.workdir), '--site', 'example',
                            '--issue', 'DEMO-42', '--profile', 'demo-reader', isolated_stdlib=True)
    assert code == 0 and out['ok'] is True
