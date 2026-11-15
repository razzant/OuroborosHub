"""chrome_browser regression tests that need no browser, run through pytest like every other hosted suite.

Node's own runner (node:test) runs the files next to this module: the request guard's wire and fail-closed
behaviour, the trusted-input preparations and delivery verdicts, a click of unknown effect sent once through the
pinned Playwright's own pointer path, the pinned patch against the stock bundle, and the connection and server over
Playwright's own consumers with a stand-in Chrome. README.md says where they came from and what was left out.
The setup cases run `setup.mjs status` on a copy of the skill: the older-host import follows the manifest's one
`mcp_servers` declaration; unsupported helper inputs get a reason and nothing to import, not a host-validation verdict.

Node with --test-force-exit and --test-timeout must be on PATH (verified on 24, used in CI). The last three files need the npm files that
skills/chrome_browser/pins.json records (OUROBOROS_CHROME_NODE_MODULES; the hosted workflow installs it). Without it they
fail and say what to install. The Python setup cases also require PyYAML. Nothing here skips.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess

import pytest

HERE = Path(__file__).resolve().parent
# Tests each file must still run. Node counts a file that registers none as one passing test, so an emptied file would pass.
MINIMUM_TESTS = {'unit_guard.test.mjs': 10, 'unit_input.test.mjs': 10, 'unit_click_once.test.mjs': 5, 'unit_patch_pinned.test.mjs': 9, 'unit_server.test.mjs': 46}
FILES = tuple(MINIMUM_TESTS)
ANSI = re.compile(r'\x1b\[[0-9;]*m')
# Node's spec reporter ends with "ℹ tests 29", "ℹ pass 29", ... (the symbol is a letter to \w, so it is matched as a word).
SUMMARY = re.compile(r'^(?:\S+\s+)?(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$', re.M)


def test_every_node_test_file_is_scheduled():
    discovered = {path.name for path in HERE.glob('*.test.mjs')}
    assert discovered == set(FILES), 'Add new Node test files to MINIMUM_TESTS, and remove deleted ones'


@pytest.mark.parametrize('name', FILES)
def test_node_file_passes_without_skips(name):
    node = shutil.which('node')
    assert node, 'Node must be on PATH (CI uses 24); these tests are Node\'s own and do not skip without it'
    # --test-force-exit: a failed assertion can leave a mock server open; the run must end and say what failed.
    # --test-timeout: a test that never ends fails by name (the whole file gets 300 s) instead of hanging the job.
    try:
        run = subprocess.run([node, '--test', '--test-force-exit', '--test-timeout=60000', '--test-reporter=spec', str(HERE / name)],
                             capture_output=True, text=True, timeout=300, cwd=HERE)
    except subprocess.TimeoutExpired as hung:
        partial = hung.stdout.decode(errors='replace') if isinstance(hung.stdout, bytes) else (hung.stdout or '')
        pytest.fail(f'{name} did not finish within 300 s:\n{ANSI.sub("", partial)}')
    stdout = ANSI.sub('', run.stdout)
    shown = f'\n{stdout}\n{ANSI.sub("", run.stderr)}'
    assert run.returncode == 0, f'{name} failed (exit {run.returncode}):{shown}'
    found = {key: int(value) for key, value in SUMMARY.findall(stdout)}
    counts = {key: found.get(key, 0) for key in ('tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo')}
    assert counts['tests'] >= MINIMUM_TESTS[name], \
        f'{name} ran {counts["tests"]} tests, at least {MINIMUM_TESTS[name]} expected (an emptied file, or a changed summary format):{shown}'
    # A skipped, todo or cancelled test is not a pass: this suite never skips.
    assert counts['pass'] == counts['tests'] and not (counts['fail'] or counts['cancelled'] or counts['skipped'] or counts['todo']), \
        f'{name}: {counts}{shown}'


@pytest.mark.parametrize('alternate', [False, True])
def test_setup_fallback_projects_manifest(tmp_path, alternate):
    """The old-host export and new-host declaration describe the same server."""
    import yaml

    source = HERE.parents[1] / 'skills' / 'chrome_browser'
    skill = tmp_path / 'chrome_browser'
    shutil.copytree(source, skill)
    manifest = skill / 'SKILL.md'
    text = manifest.read_text()
    data = yaml.safe_load(text.split('---', 2)[1])
    declaration = data['mcp_servers'][0]
    if alternate:
        changed = dict(declaration, id='another_chrome', title='Changed title',
                       entry='scripts/alternate.mjs', browser_policy=False)
        text = text.replace(json.dumps(data['mcp_servers'], separators=(',', ':')),
                            json.dumps([changed], separators=(',', ':')))
        manifest.write_text(text)
        # A declared entry must be a file in the skill, as a supporting host requires.
        (skill / changed['entry']).write_text('')
        declaration = changed
    node = shutil.which('node')
    assert node, 'Node is required'
    env = dict(os.environ, OUROBOROS_CHROME_NODE_MODULES=str(tmp_path / 'missing'))
    run = subprocess.run([node, str(skill / 'scripts' / 'setup.mjs'), 'status'],
                         cwd=skill, env=env, capture_output=True, text=True, timeout=30)
    # No dependencies were installed in this isolated fixture: useful status, not readiness.
    assert run.returncode == 1, run.stderr
    report = json.loads(run.stdout)
    assert report['ready'] is False
    exported = report['mcp_import_document']['mcpServers']
    assert set(exported) == {declaration['id']}
    server = exported[declaration['id']]
    assert server['args'] == [str(skill / declaration['entry'])]
    assert server['task_session'] == declaration['task_session']
    assert server['browser_policy'] == declaration['browser_policy']
    assert report['mcp_server_entry']['name'] == declaration['title']
    assert 'Connect tools' in report['connection']['preferred']
    assert report['owner_steps'], 'Keep the older-host consumer field'


@pytest.mark.parametrize('change, problem', [
    ('[{id: chrome_browser, entry: scripts/server.mjs}]', 'one line of JSON'),
    ([{}, {'id': 'second_chrome'}], 'exactly one server'),
    ({'command': 'node'}, 'unknown keys command'),
    ({'id': 'Chrome-Browser'}, 'id "Chrome-Browser"'),
    ({'id': 'a' * 25}, 'at most 24 characters'),
    ({'entry': '../chrome_browser/scripts/server.mjs'}, 'entry "../chrome_browser/scripts/server.mjs"'),
    ({'entry': 'scripts/missing.mjs'}, 'entry "scripts/missing.mjs"'),
    ({'entry': '.ouroboros_env/node/server.mjs'}, 'outside dependency and cache folders'),
    ({'runtime': 'python3'}, 'node runtime'),
    ({'runtime': None}, 'node runtime'),
    ({'runtime': '"node"'}, 'node runtime'),
    ({'task_session': 'yes'}, 'true or false'),
    ({'task_session': False}, 'browser_policy without task_session'),
], ids=['not-json', 'two-servers', 'unknown-key', 'id', 'long-id', 'entry-escapes', 'entry-missing',
        'entry-in-dependencies', 'runtime', 'runtime-null', 'runtime-literal-quotes', 'flag-type', 'policy-without-session'])
def test_setup_reports_an_invalid_declaration_and_offers_no_import(tmp_path, change, problem):
    """Unsupported input gets a reason and no import; the helper does not validate general YAML."""
    import yaml

    skill = tmp_path / 'chrome_browser'
    shutil.copytree(HERE.parents[1] / 'skills' / 'chrome_browser', skill)
    manifest = skill / 'SKILL.md'
    text = manifest.read_text(encoding='utf-8')
    published = yaml.safe_load(text.split('---', 2)[1])['mcp_servers'][0]
    changes = change if isinstance(change, list) else [change]
    value = change if isinstance(change, str) else json.dumps([dict(published, **item) for item in changes])
    line = next(line for line in text.splitlines() if line.startswith('mcp_servers: '))
    manifest.write_text(text.replace(line, f'mcp_servers: {value}'), encoding='utf-8')
    # This file exists, so only the dependency-folder rule can refuse it.
    (skill / '.ouroboros_env' / 'node').mkdir(parents=True)
    (skill / '.ouroboros_env' / 'node' / 'server.mjs').write_text('')
    node = shutil.which('node')
    assert node, 'Node is required'
    env = dict(os.environ, OUROBOROS_CHROME_NODE_MODULES=str(tmp_path / 'missing'))
    run = subprocess.run([node, str(skill / 'scripts' / 'setup.mjs'), 'status'],
                         cwd=skill, env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == 1, run.stderr
    report = json.loads(run.stdout)
    assert report['ready'] is False
    assert report['error']['code'] == 'CHROME_BROWSER_DECLARATION_INVALID'
    assert problem in report['error']['problem'], report['error']
    assert report['dependencies']['ok'] is False, 'the dependency status is still reported'
    assert not {'mcp_import_document', 'mcp_server_entry', 'owner_steps'} & report.keys()
